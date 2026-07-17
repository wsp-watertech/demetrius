"""TNM Access API client for querying USGS 3DEP tiles."""

import logging
from datetime import datetime
from typing import Any

import httpx

from .models import BoundingBox, Tile
from .parser import parse_dataset_and_tile_ids
from .sources import TileSource

logger = logging.getLogger(__name__)

TNM_API_BASE = "https://tnmaccess.nationalmap.gov/api/v1/products"


class TNMTileSource(TileSource):
    """Query USGS TNM Access API for 3DEP DEM tiles."""

    def __init__(self, timeout: float = 30.0):
        """Initialize TNM client.

        Parameters
        ----------
        timeout : float, default=30.0
            Request timeout in seconds.

        Returns
        -------
        None
            Initializes the TNM tile source.
        """
        self.timeout = timeout
        self.client = httpx.Client(timeout=timeout)

    def search(self, aoi_bbox: BoundingBox) -> list[Tile]:
        """Search TNM for tiles intersecting bounding box.

        Paginates through all results from TNM Access API (backed by ScienceBase).

        Parameters
        ----------
        aoi_bbox : BoundingBox
            Bounding box in EPSG:4326.

        Returns
        -------
        list[Tile]
            Tile objects returned from TNM.

        Raises
        ------
        ValueError
            If the TNM query fails or returns invalid JSON.
        """
        logger.info(
            f"Querying TNM for tiles in bbox: {aoi_bbox.min_x}, {aoi_bbox.min_y}, "
            f"{aoi_bbox.max_x}, {aoi_bbox.max_y}"
        )

        tiles = []
        offset = 0
        page_size = 50  # Max items per request (TNM default is 50)

        while True:
            params = {
                "bbox": f"{aoi_bbox.min_x},{aoi_bbox.min_y},{aoi_bbox.max_x},{aoi_bbox.max_y}",
                "datasets": "Digital Elevation Model (DEM) 1 meter",
                "prodFormats": "GeoTIFF",
                "outputFormat": "JSON",
                "max": page_size,
                "offset": offset,
            }

            try:
                response = self.client.get(TNM_API_BASE, params=params)
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise ValueError(f"TNM API request failed: {e}") from e

            try:
                data = response.json()
            except ValueError as e:
                raise ValueError(f"TNM API returned invalid JSON: {e}") from e

            total = data.get("total", 0)
            items = data.get("items", [])

            logger.debug(f"Fetched {len(items)} items (offset={offset}, total={total})")

            # Parse items on this page
            for item in items:
                try:
                    tile = self._parse_tnm_item(item)
                    tiles.append(tile)
                    logger.debug(f"Parsed tile: {tile.id}")
                except ValueError as e:
                    logger.warning(f"Skipped TNM item: {e}")
                    continue

            # Check if we've fetched all items
            if offset + len(items) >= total or not items:
                break

            offset += page_size

        logger.info(f"Found {len(tiles)} tiles from TNM (total {total} items)")
        return tiles

    def _parse_tnm_item(self, item: dict[str, Any]) -> Tile:
        """Parse a single TNM product item into Tile object.

        Parameters
        ----------
        item : dict[str, Any]
            TNM API response item.

        Returns
        -------
        Tile
            Parsed tile object.

        Raises
        ------
        ValueError
            If required fields are missing or invalid.
        """
        required_fields = ["title", "downloadURL", "publicationDate", "lastUpdated", "boundingBox"]
        for field in required_fields:
            if field not in item:
                raise ValueError(f"Missing required field: {field}")

        title = item["title"]
        url = item["downloadURL"]
        bbox = item["boundingBox"]

        # Parse dataset and tile IDs from title
        try:
            dataset_id, tile_id = parse_dataset_and_tile_ids(title, url)
        except ValueError as e:
            raise ValueError(f"Failed to parse IDs from '{title}': {e}") from e

        # Parse dates
        try:
            pub_date = self._parse_date(item["publicationDate"])
            last_updated = self._parse_date(item["lastUpdated"])
        except ValueError as e:
            raise ValueError(f"Invalid date format: {e}") from e

        # Validate bounding box
        try:
            bounds = BoundingBox(
                min_x=float(bbox["minX"]),
                min_y=float(bbox["minY"]),
                max_x=float(bbox["maxX"]),
                max_y=float(bbox["maxY"]),
            )
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"Invalid boundingBox: {e}") from e

        # Create tile ID
        tile_full_id = f"{dataset_id}_{tile_id}"

        return Tile(
            id=tile_full_id,
            dataset_id=dataset_id,
            tile_id=tile_id,
            publication_date=pub_date,
            last_updated=last_updated,
            download_url=url,
            bounds_wgs84=bounds,
            priority=-1,  # Will be assigned during prioritization
        )

    def _parse_date(self, date_str: str) -> datetime:
        """Parse date string from TNM response.

        TNM uses formats like:
        - "2021-11-18" (date only)
        - "2021-11-22T17:32:57" (ISO with time)
        - "2021-11-22T17:32:57.123" (ISO with milliseconds)
        - "2022-05-24T23:02:12.343-06:00" (ISO8601 with timezone)

        Parameters
        ----------
        date_str : str
            Date string from TNM.

        Returns
        -------
        datetime
            Parsed datetime object with timezone information removed.

        Raises
        ------
        ValueError
            If the date string cannot be parsed.
        """
        # Remove timezone info if present (anything after +/-)
        if "+" in date_str or date_str.count("-") > 2:
            # Has timezone offset like -06:00
            date_str = (
                date_str.split("+")[0].split("-")[0]
                if "+" in date_str
                else date_str.rsplit("-", 1)[0]
            )

        # Try ISO format first (with or without time/milliseconds)
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",  # with milliseconds
            "%Y-%m-%dT%H:%M:%S",  # without milliseconds
            "%Y-%m-%d",  # date only
        ]:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        raise ValueError(f"Could not parse date: {date_str}")

    def __del__(self) -> None:
        """Clean up the HTTP client.

        Returns
        -------
        None
            Closes the HTTP client when the instance is destroyed.
        """
        if hasattr(self, "client"):
            self.client.close()
