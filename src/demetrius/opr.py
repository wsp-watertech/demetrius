"""Explicitly selected OPR DEM projects from the USGS Elevation Index."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

import geopandas as gpd
import httpx
from shapely.geometry import shape

from .models import BoundingBox, Tile
from .project_boundaries import ProjectBoundaries
from .tnm import TNMTileSource
from .usgs_index import FootprintQuery

logger = logging.getLogger(__name__)

OPR_PROJECTS_URL = (
    "https://index.nationalmap.gov/arcgis/rest/services/3DEPElevationIndex/MapServer/27/query"
)
OPR_DATASET = "Original Product Resolution (OPR) Digital Elevation Model (DEM)"
OPR_PAGE_SIZE = 1000


def _date(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value / 1000, UTC).replace(tzinfo=None) if value else None


def query_projects(
    bbox: BoundingBox | None = None, project_ids: Sequence[int] = ()
) -> list[dict[str, Any]]:
    """Fetch OPR workunit footprints by stable project ID or AOI."""
    params: dict[str, str | int] = {
        "where": (
            f"project_id IN ({','.join(str(int(pid)) for pid in project_ids)})"
            if project_ids
            else "1=1"
        ),
        "outFields": (
            "project,project_id,workunit,workunit_id,collect_start,collect_end,"
            "dem_gsd_meters,horiz_crs,vert_crs,sourcedem_link"
        ),
        "returnGeometry": "true",
        "outSR": 4326,
        "orderByFields": "OBJECTID ASC",
        "resultRecordCount": OPR_PAGE_SIZE,
        "f": "geojson",
    }
    if bbox is not None:
        params.update(
            {
                "geometry": ",".join(str(v) for v in bbox.as_tuple()),
                "geometryType": "esriGeometryEnvelope",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
            }
        )
    with httpx.Client(timeout=60, transport=httpx.HTTPTransport(retries=3)) as client:
        features = FootprintQuery(
            client,
            OPR_PROJECTS_URL,
            "OPR project",
            ("project", "project_id", "workunit", "workunit_id"),
            OPR_PAGE_SIZE,
        ).fetch(params)
    missing = set(project_ids) - {f["properties"]["project_id"] for f in features}
    if missing:
        raise ValueError(f"Unknown OPR project ID(s): {', '.join(map(str, sorted(missing)))}")
    return features


def _project_path(url: str) -> tuple[str, str] | None:
    """Read exact project/workunit directory names, not product-title guesses."""
    parts = PurePosixPath(unquote(urlparse(url).path)).parts
    try:
        i = parts.index("Projects")
        if parts[i - 1] != "OPR":
            return None
        return parts[i + 1], parts[i + 2]
    except (ValueError, IndexError):
        return None


class OPRTileSource(TNMTileSource):
    """Discover only products from explicitly selected catalog projects."""

    dataset_name = OPR_DATASET
    product_formats = "GeoTIFF,IMG"
    strict_parsing = True

    def __init__(self, project_ids: Sequence[int]):
        super().__init__()
        if not project_ids or any(pid <= 0 for pid in project_ids):
            raise ValueError("Select one or more positive OPR catalog project IDs")
        self.features = query_projects(project_ids=project_ids)
        self.workunits: dict[tuple[str, str], dict[str, Any]] = {}
        for feature in self.features:
            props = feature["properties"]
            self.workunits[(props["project"], props["workunit"])] = props

    def search(self, aoi_bbox: BoundingBox) -> list[Tile]:
        """Report unavailable selected workunits rather than silently omitting them."""
        tiles = super().search(aoi_bbox)
        found = {(tile.project_id, tile.workunit) for tile in tiles}
        for feature in self.features:
            props = feature["properties"]
            if (
                shape(feature["geometry"]).intersects(aoi_bbox.to_polygon())
                and (props["project_id"], props["workunit"]) not in found
            ):
                raise ValueError(
                    f"No OPR DEM tiles found for selected workunit {props['workunit']} "
                    f"(project {props['project_id']}) intersecting the search area"
                )
        return tiles

    def _accept_item(self, item: dict[str, Any]) -> bool:
        return _project_path(item.get("downloadURL", "")) in self.workunits

    def _parse_tnm_item(self, item: dict[str, Any]) -> Tile:
        for field in ("downloadURL", "sourceId", "publicationDate", "lastUpdated", "boundingBox"):
            if field not in item:
                raise ValueError(f"Missing OPR product field: {field}")
        key = _project_path(item["downloadURL"])
        if key not in self.workunits:
            raise ValueError("OPR product is not in a selected workunit")
        props = self.workunits[key]
        source_id = item.get("sourceId")
        if (
            not isinstance(source_id, str)
            or not source_id
            or not all(c.isascii() and (c.isalnum() or c in "_-") for c in source_id)
        ):
            raise ValueError("OPR product has no safe stable sourceId")
        start, end = self._fetch_sciencebase_flight_dates(item.get("metaUrl"))
        start = start or _date(props.get("collect_start"))
        end = end or _date(props.get("collect_end"))
        if start is not None and end is not None and start > end:
            raise ValueError(f"OPR flight end precedes start for {source_id}")
        if start is None or end is None:
            logger.warning("Incomplete OPR flight dates for %s", source_id)
        dataset_id = f"opr_{props['project_id']}_{props['workunit_id']}"
        bbox = item["boundingBox"]
        return Tile(
            id=f"{dataset_id}_{source_id}",
            dataset_id=dataset_id,
            tile_id=source_id,
            publication_date=self._parse_date(item["publicationDate"]),
            last_updated=self._parse_date(item["lastUpdated"]),
            flight_start=start,
            flight_end=end,
            download_url=item["downloadURL"],
            bounds_wgs84=BoundingBox(
                min_x=bbox["minX"],
                min_y=bbox["minY"],
                max_x=bbox["maxX"],
                max_y=bbox["maxY"],
            ),
            priority=-1,
            product="opr",
            project_id=props["project_id"],
            workunit=props["workunit"],
            source_id=source_id,
            metadata_url=item.get("metaUrl"),
            source_crs=f"EPSG:{props['horiz_crs']}" if props.get("horiz_crs") else None,
            vertical_crs=f"EPSG:{props['vert_crs']}" if props.get("vert_crs") else None,
            resolution_m=props.get("dem_gsd_meters"),
        )

    def footprints(self, tiles: Sequence[Tile]) -> ProjectBoundaries | None:
        """Limit coverage to workunit footprints intersected with discovered tiles."""
        features = []
        for feature in self.features:
            props = feature["properties"]
            polygons = [
                tile.bounds_wgs84.to_polygon()
                for tile in tiles
                if tile.project_id == props["project_id"] and tile.workunit == props["workunit"]
            ]
            if polygons:
                from shapely.ops import unary_union

                geometry = shape(feature["geometry"]).intersection(unary_union(polygons))
                if not geometry.is_empty:
                    features.append({"geometry": geometry})
        return ProjectBoundaries(gpd.GeoDataFrame(features, crs="EPSG:4326")) if features else None


def combine_footprints(
    first: ProjectBoundaries | None, second: ProjectBoundaries | None
) -> ProjectBoundaries | None:
    """Combine source footprints without substituting 1m footprints for OPR."""
    if first is None:
        return second
    if second is None:
        return first
    return ProjectBoundaries(
        gpd.GeoDataFrame(
            geometry=[*first.gdf.to_crs(4326).geometry, *second.gdf.to_crs(4326).geometry],
            crs="EPSG:4326",
        )
    )
