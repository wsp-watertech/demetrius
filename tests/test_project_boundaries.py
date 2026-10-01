"""USGS project footprint lookup and pipeline integration."""

from datetime import UTC, datetime
from unittest.mock import patch

import geopandas as gpd
import httpx
import pytest
from click.testing import CliRunner
from shapely.geometry import box, mapping

from src.demetrius import project_boundaries
from src.demetrius.cli import process
from src.demetrius.models import AOI, BoundingBox, Tile
from src.demetrius.pipeline import PipelineResult, run_pipeline
from src.demetrius.project_boundaries import ProjectBoundaries


def _feature(geometry, name):
    return {
        "type": "Feature",
        "geometry": mapping(geometry),
        "properties": {"project": name, "project_id": 1},
    }


def _mock_usgs(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        project_boundaries.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_usgs_footprints_query_pages_and_uses_wgs84(monkeypatch):
    monkeypatch.setattr(project_boundaries, "USGS_PAGE_SIZE", 2)
    requests = []
    pages = [
        [
            _feature(box(-74.5, 40.0, -74.45, 40.1), "first"),
            _feature(box(-74.45, 40.0, -74.4, 40.1), "second"),
        ],
        [_feature(box(-74.4, 40.0, -74.35, 40.1), "third")],
    ]

    def handler(request):
        requests.append(request)
        offset = int(request.url.params["resultOffset"])
        return httpx.Response(
            200, json={"type": "FeatureCollection", "features": pages[offset // 2]}
        )

    _mock_usgs(monkeypatch, handler)
    bbox = BoundingBox(min_x=-74.5, min_y=40, max_x=-74.35, max_y=40.1)
    bounds = ProjectBoundaries.from_usgs(bbox)

    assert len(bounds.gdf) == 3
    assert bounds.gdf.crs.to_epsg() == 4326
    assert bounds.covers_geometry(box(-74.5, 40.0, -74.35, 40.1))
    assert [r.url.params["resultOffset"] for r in requests] == ["0", "2"]
    assert requests[0].url.path.endswith("/MapServer/18/query")
    assert requests[0].url.params["geometry"] == "-74.5,40.0,-74.35,40.1"
    assert requests[0].url.params["outSR"] == "4326"
    assert requests[0].url.params["returnGeometry"] == "true"
    assert requests[0].url.params["orderByFields"] == "OBJECTID ASC"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"type": "FeatureCollection", "features": []}, "No USGS"),
        ({"error": {"message": "Invalid request"}}, "invalid GeoJSON"),
        ({"features": [{"type": "Feature", "geometry": None}]}, "invalid GeoJSON"),
    ],
)
def test_usgs_footprints_rejects_missing_or_invalid_data(monkeypatch, body, message):
    _mock_usgs(monkeypatch, lambda request: httpx.Response(200, json=body))
    bbox = BoundingBox(min_x=-74.5, min_y=40, max_x=-74.4, max_y=40.1)

    with pytest.raises(ValueError, match=message):
        ProjectBoundaries.from_usgs(bbox)


def test_usgs_footprints_http_error_is_not_silently_ignored(monkeypatch):
    _mock_usgs(monkeypatch, lambda request: httpx.Response(403))
    bbox = BoundingBox(min_x=-74.5, min_y=40, max_x=-74.4, max_y=40.1)

    with pytest.raises(ValueError, match="USGS project footprint query failed"):
        ProjectBoundaries.from_usgs(bbox)


def test_project_intersection_checks_geometry_not_only_bounds():
    from shapely.geometry import Polygon

    triangle = Polygon([(0, 0), (2, 0), (0, 2)])
    bounds = ProjectBoundaries(gpd.GeoDataFrame(geometry=[triangle], crs="EPSG:4326"))
    assert not bounds.intersects_coverage(box(1.5, 1.5, 1.8, 1.8))
    assert bounds.intersects_coverage(box(0.1, 0.1, 0.3, 0.3))


def test_buffered_project_intersection_uses_union():
    bounds = ProjectBoundaries(
        gpd.GeoDataFrame(
            geometry=[box(-75, 40, -74.99, 40.01), box(-74.98, 40, -74.97, 40.01)],
            crs="EPSG:4326",
        )
    )
    assert bounds.intersects_coverage_with_buffer(box(-75, 40, -74.99, 40.01), 100)


@pytest.fixture
def sample_job():
    aoi = AOI(geometry=box(-74.5, 40, -74.4, 40.1))
    tile = Tile(
        id="test_tile",
        dataset_id="test_project",
        tile_id="x0y0",
        publication_date=datetime(2021, 1, 1, tzinfo=UTC),
        last_updated=datetime(2021, 1, 1, tzinfo=UTC),
        download_url="https://example.com/test.tif",
        bounds_wgs84=aoi.bounds(),
        priority=0,
    )
    bounds = ProjectBoundaries(gpd.GeoDataFrame(geometry=[aoi.geometry], crs="EPSG:4326"))
    return aoi, tile, bounds


def test_pipeline_queries_usgs_by_default(sample_job, tmp_path):
    aoi, tile, bounds = sample_job
    with (
        patch("src.demetrius.tnm.TNMTileSource") as source,
        patch.object(ProjectBoundaries, "from_usgs", return_value=bounds) as usgs,
    ):
        source.return_value.search.return_value = [tile]
        result = run_pipeline(
            aoi, tmp_path / "dem.tif", output_crs="EPSG:32618", mode="download-only"
        )

    assert result.status == "success"
    assert result.tile_count == 1
    usgs.assert_called_once_with(aoi.bounds())


def test_pipeline_explicit_file_skips_usgs(sample_job, tmp_path):
    aoi, tile, bounds = sample_job
    with (
        patch("src.demetrius.tnm.TNMTileSource") as source,
        patch.object(ProjectBoundaries, "from_file", return_value=bounds) as from_file,
        patch.object(ProjectBoundaries, "from_usgs") as usgs,
    ):
        source.return_value.search.return_value = [tile]
        result = run_pipeline(
            aoi,
            tmp_path / "dem.tif",
            output_crs="EPSG:32618",
            project_bounds="bounds.gpkg",
            mode="download-only",
        )

    assert result.status == "success"
    from_file.assert_called_once_with("bounds.gpkg")
    usgs.assert_not_called()


def test_pipeline_reports_usgs_failure(sample_job, tmp_path):
    aoi, tile, _ = sample_job
    with (
        patch("src.demetrius.tnm.TNMTileSource") as source,
        patch.object(ProjectBoundaries, "from_usgs", side_effect=ValueError("USGS unavailable")),
    ):
        source.return_value.search.return_value = [tile]
        result = run_pipeline(
            aoi, tmp_path / "dem.tif", output_crs="EPSG:32618", mode="download-only"
        )

    assert result.status == "failed"
    assert "USGS unavailable" in result.error


def test_pipeline_checks_default_project_coverage(sample_job, tmp_path):
    aoi, tile, _ = sample_job
    bounds = ProjectBoundaries(
        gpd.GeoDataFrame(geometry=[box(-74.5, 40, -74.45, 40.1)], crs="EPSG:4326")
    )
    with (
        patch("src.demetrius.tnm.TNMTileSource") as source,
        patch.object(ProjectBoundaries, "from_usgs", return_value=bounds),
    ):
        source.return_value.search.return_value = [tile]
        result = run_pipeline(
            aoi,
            tmp_path / "dem.tif",
            output_crs="EPSG:32618",
            require_full_coverage=True,
            mode="download-only",
        )

    assert result.status == "failed"
    assert "50.0% covered by project boundaries" in result.error


def test_process_cli_accepts_missing_project_bounds(tmp_path):
    aoi = tmp_path / "aoi.geojson"
    aoi.write_text('{"type":"FeatureCollection","features":[]}')
    with patch(
        "src.demetrius.pipeline.run_pipeline",
        return_value=PipelineResult(name="dem", status="success"),
    ) as pipeline:
        result = CliRunner().invoke(process, ["--aoi", str(aoi), "--mode", "download-only"])

    assert result.exit_code == 0
    assert pipeline.call_args.kwargs["project_bounds"] is None
