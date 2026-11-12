"""Selected OPR discovery, planning, and mixed-source raster assembly."""

import json
from datetime import datetime
from unittest.mock import patch

import geopandas as gpd
import httpx
import numpy as np
import pyproj
import pytest
from click.testing import CliRunner
from osgeo import gdal
from shapely.geometry import Polygon, box, mapping, shape

from src.demetrius import opr, usgs_index
from src.demetrius.cli import cli
from src.demetrius.crs import get_crs_from_raster
from src.demetrius.manifest import Manifest
from src.demetrius.models import AOI, BoundingBox, Tile
from src.demetrius.opr_normalizer import normalize_opr_tiles
from src.demetrius.pipeline import PipelineResult, run_pipeline
from src.demetrius.priority import prioritize_datasets
from src.demetrius.project_boundaries import ProjectBoundaries


def _feature(project_id=300169, workunit_id=300431):
    return {
        "type": "Feature",
        "geometry": mapping(box(-85, 37, -83, 39)),
        "properties": {
            "project": "KY_CentralEast_A23",
            "project_id": project_id,
            "workunit": "KY_CentralEast_2_A23",
            "workunit_id": workunit_id,
            "collect_start": 1671408000000,
            "collect_end": 1677974400000,
            "dem_gsd_meters": 0.609601219202438,
            "horiz_crs": "6473",
            "vert_crs": "6360",
        },
    }


def _item(source_id="66ced0b8d34e98e8a924a8dd", project="KY_CentralEast_A23"):
    return {
        "title": f"USGS Original Product Resolution {project} N116E324",
        "sourceId": source_id,
        "downloadURL": (
            "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/OPR/Projects/"
            f"{project}/KY_CentralEast_2_A23/TIFF/USGS_OPR_{source_id}.tif"
        ),
        "publicationDate": "2024-08-26",
        "lastUpdated": "2024-08-28T01:24:41.704-06:00",
        "boundingBox": {"minX": -84.12, "minY": 37.69, "maxX": -84.09, "maxY": 37.71},
    }


def _tile(dataset="opr_300169_300431", product="opr", year=2023):
    return Tile(
        id=f"{dataset}_source",
        dataset_id=dataset,
        tile_id="source",
        product=product,
        project_id=300169 if product == "opr" else None,
        workunit="KY_CentralEast_2_A23" if product == "opr" else None,
        flight_start=datetime.fromisoformat(f"{year}-01-01"),
        flight_end=datetime.fromisoformat(f"{year}-03-05"),
        publication_date=datetime.fromisoformat("2024-08-26"),
        last_updated=datetime.fromisoformat("2024-08-28"),
        download_url="https://example.com/dem.tif",
        bounds_wgs84=BoundingBox(min_x=-84.12, min_y=37.69, max_x=-84.09, max_y=37.71),
        priority=-1,
        vertical_crs="EPSG:6360" if product == "opr" else None,
        source_crs="EPSG:6473" if product == "opr" else None,
        resolution_m=0.609601219202438 if product == "opr" else None,
    )


def test_project_lookup_pages_and_validates_exact_ids(monkeypatch):
    requests = []
    client = httpx.Client

    def handler(request):
        requests.append(request)
        offset = int(request.url.params["resultOffset"])
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [_feature(workunit_id=offset + 1)],
                "exceededTransferLimit": offset == 0,
            },
        )

    def mock_client(**kwargs):
        kwargs.pop("transport", None)
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(opr.httpx, "Client", mock_client)
    assert len(opr.query_projects(project_ids=[300169])) == 2
    assert [r.url.params["resultOffset"] for r in requests] == ["0", "1"]
    assert requests[0].url.params["where"] == "project_id IN (300169)"
    assert requests[0].url.params["outSR"] == "4326"
    with pytest.raises(ValueError, match="Unknown OPR project ID"):
        opr.query_projects(project_ids=[999])


def _mock_opr_client(monkeypatch, handler):
    client = httpx.Client

    def mock_client(**kwargs):
        kwargs.pop("transport", None)
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(opr.httpx, "Client", mock_client)
    monkeypatch.setattr(usgs_index.time, "sleep", lambda _: None)


def test_project_lookup_retries_transient_server_failure_without_fallback(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"type": "FeatureCollection", "features": [_feature()]})

    _mock_opr_client(monkeypatch, handler)
    assert len(opr.query_projects(project_ids=[300169])) == 1
    assert len(requests) == 2
    assert all(r.url.params["returnGeometry"] == "true" for r in requests)


@pytest.mark.parametrize("selection", ["bbox", "project_ids"])
def test_project_lookup_falls_back_to_paginated_attributes_and_individual_footprints(
    monkeypatch, selection
):
    requests = []
    features = [_feature(workunit_id=1), _feature(workunit_id=2)]

    def handler(request):
        requests.append(request)
        params = request.url.params
        if params.get("objectIds"):
            index = int(params["objectIds"]) - 10
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": features[index]["geometry"],
                            "properties": {"OBJECTID": index + 10},
                        }
                    ],
                },
            )
        if params["returnGeometry"] == "false":
            index = int(params["resultOffset"])
            return httpx.Response(
                200,
                json={
                    "features": [
                        {"attributes": {"OBJECTID": index + 10, **features[index]["properties"]}}
                    ],
                    "exceededTransferLimit": index == 0,
                },
            )
        if int(params["resultOffset"]) == 0:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [features[0]],
                    "exceededTransferLimit": True,
                },
            )
        return httpx.Response(200, json={"error": {"code": 500, "message": "query failed"}})

    _mock_opr_client(monkeypatch, handler)
    kwargs = (
        {"bbox": BoundingBox(min_x=-85, min_y=37, max_x=-83, max_y=39)}
        if selection == "bbox"
        else {"project_ids": [300169]}
    )
    result = opr.query_projects(**kwargs)
    assert [feature["properties"]["workunit_id"] for feature in result] == [1, 2]
    assert all(
        shape(actual["geometry"]).equals(shape(expected["geometry"]))
        for actual, expected in zip(result, features, strict=True)
    )
    assert (
        sum(
            r.url.params.get("resultOffset") == "1" and r.url.params["returnGeometry"] == "true"
            for r in requests
        )
        == 3
    )
    assert [
        r.url.params["resultOffset"] for r in requests if r.url.params["returnGeometry"] == "false"
    ] == ["0", "1"]
    footprint_requests = [r for r in requests if r.url.params.get("objectIds")]
    assert [r.url.params["objectIds"] for r in footprint_requests] == ["10", "11"]
    assert all(r.url.params["outSR"] == "4326" for r in requests)
    if selection == "bbox":
        assert all(r.url.params["geometry"] == "-85.0,37.0,-83.0,39.0" for r in requests)
    else:
        assert all(r.url.params["where"] == "project_id IN (300169)" for r in requests)


@pytest.mark.parametrize("failure", ["server", "missing", "wrong_id"])
def test_individual_footprint_failure_is_explicit(monkeypatch, failure):
    def handler(request):
        params = request.url.params
        if params.get("objectIds"):
            if failure == "server":
                return httpx.Response(503)
            items = (
                []
                if failure == "missing"
                else [
                    {
                        "type": "Feature",
                        "geometry": _feature()["geometry"],
                        "properties": {"OBJECTID": 999},
                    }
                ]
            )
            return httpx.Response(200, json={"type": "FeatureCollection", "features": items})
        if params["returnGeometry"] == "false":
            return httpx.Response(
                200,
                json={"features": [{"attributes": {"OBJECTID": 10, **_feature()["properties"]}}]},
            )
        return httpx.Response(500)

    _mock_opr_client(monkeypatch, handler)
    with pytest.raises(ValueError, match="OPR project footprint.*OBJECTID 10"):
        opr.query_projects(project_ids=[300169])


def test_project_lookup_does_not_fallback_on_client_error(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(403)

    _mock_opr_client(monkeypatch, handler)
    with pytest.raises(ValueError, match="403"):
        opr.query_projects(project_ids=[300169])
    assert len(requests) == 1


def test_opr_search_accepts_only_selected_project_and_nonstandard_tile_names(monkeypatch):
    monkeypatch.setattr(opr, "query_projects", lambda **kwargs: [_feature()])
    source = opr.OPRTileSource([300169])
    source.client.close()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "total": 3,
                "items": [
                    _item(),
                    _item(project="Unselected"),
                    _item(project="KY_CentralEast_A230"),
                ],
            },
        )

    source.client = httpx.Client(transport=httpx.MockTransport(handler))
    tiles = source.search(_tile().bounds_wgs84)
    assert len(tiles) == 1
    tile = tiles[0]
    assert tile.dataset_id == "opr_300169_300431"
    assert tile.tile_id == "66ced0b8d34e98e8a924a8dd"
    assert tile.flight_start == datetime.fromisoformat("2022-12-19")
    assert tile.flight_end == datetime.fromisoformat("2023-03-05")
    assert tile.vertical_crs == "EPSG:6360"
    assert tile.resolution_m < 1
    assert requests[0].url.params["datasets"] == opr.OPR_DATASET
    source.client.close()


def test_invalid_selected_opr_product_is_not_silently_skipped(monkeypatch):
    monkeypatch.setattr(opr, "query_projects", lambda **kwargs: [_feature()])
    source = opr.OPRTileSource([300169])
    source.client.close()
    source.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"total": 1, "items": [_item("../unsafe")]})
        )
    )
    with pytest.raises(ValueError, match="Invalid selected OPR"):
        source.search(_tile().bounds_wgs84)
    source.client.close()


def test_missing_selected_workunit_is_an_error_but_nonintersecting_projects_are_allowed(
    monkeypatch,
):
    monkeypatch.setattr(opr, "query_projects", lambda **kwargs: [_feature()])
    source = opr.OPRTileSource([300169])
    source.client.close()
    source.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"total": 0, "items": []})
        )
    )
    with pytest.raises(ValueError, match="No OPR DEM tiles found"):
        source.search(_tile().bounds_wgs84)
    assert source.search(BoundingBox(min_x=-76, min_y=40, max_x=-75, max_y=41)) == []
    source.client.close()


def test_mixed_priority_respects_flight_dates_and_prefers_1m_on_ties():
    tiles = [
        _tile("standard_new", "1m", 2024),
        _tile("standard_same_flight", "1m"),
        _tile(),
        _tile("standard_old", "1m", 2020),
    ]
    assert [t.dataset_id for t in prioritize_datasets(tiles)] == [
        "standard_old",
        "opr_300169_300431",
        "standard_same_flight",
        "standard_new",
    ]


def _raster(path, crs, values, transform):
    with gdal.ExceptionMgr():
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(path), values.shape[1], values.shape[0], 1, gdal.GDT_Float32
        )
        dataset.SetProjection(pyproj.CRS(crs).to_wkt())
        dataset.SetGeoTransform(transform)
        dataset.GetRasterBand(1).SetNoDataValue(-9999)
        dataset.GetRasterBand(1).WriteArray(values)
        dataset.FlushCache()
    return str(path)


def test_normalization_scales_feet_preserves_nodata_and_does_not_change_download(tmp_path):
    values = np.full((10, 10), 100, dtype=np.float32)
    values[4:6, 4:6] = -9999
    tile = _tile()
    tile.local_path = _raster(
        tmp_path / "opr.tif", "EPSG:6473+6360", values, (5395000, 2, 0, 3785000, 0, -2)
    )
    normalized = normalize_opr_tiles([tile], tmp_path)[0]
    assert normalized.local_path != tile.local_path
    dataset = gdal.Open(normalized.local_path)
    result = dataset.ReadAsArray()
    assert result[0, 0] == pytest.approx(30.48006096)
    assert result[4, 4] == -9999
    assert not pyproj.CRS(dataset.GetProjection()).is_compound
    assert gdal.Open(tile.local_path).ReadAsArray()[0, 0] == 100
    assert get_crs_from_raster(normalized.local_path) == "EPSG:6473"


@pytest.mark.parametrize(
    ("raster_crs", "catalog_crs", "message"),
    [
        ("EPSG:6473", None, "Unknown OPR"),
        ("EPSG:6473+5773", "EPSG:5773", "Unknown or incompatible"),
        ("EPSG:6473+6360", "EPSG:5773", "Conflicting OPR"),
    ],
)
def test_normalization_rejects_unknown_or_incompatible_datums(
    tmp_path, raster_crs, catalog_crs, message
):
    tile = _tile()
    tile.vertical_crs = catalog_crs
    tile.local_path = _raster(
        tmp_path / "opr.tif", raster_crs, np.ones((2, 2)), (5395000, 2, 0, 3785000, 0, -2)
    )
    standard = _tile("standard", "1m", 2020)
    standard.local_path = _raster(
        tmp_path / "standard.tif",
        "EPSG:32616+5703",
        np.ones((2, 2)),
        (500000, 1, 0, 4000000, 0, -1),
    )
    with pytest.raises(ValueError, match=message):
        normalize_opr_tiles([standard, tile], tmp_path)


def test_normalization_accepts_geographic_sources_and_rejects_conflicting_units(tmp_path):
    tile = _tile()
    tile.local_path = _raster(
        tmp_path / "geographic.tif",
        "EPSG:4326+6360",
        np.full((2, 2), 100),
        (-84.1, 0.00001, 0, 37.7, 0, -0.00001),
    )
    normalized = normalize_opr_tiles([tile], tmp_path)[0]
    with gdal.ExceptionMgr():
        dataset = gdal.Open(normalized.local_path)
        assert dataset.ReadAsArray()[0, 0] == pytest.approx(30.48006096)
        dataset = None
        dataset = gdal.Open(tile.local_path, gdal.GA_Update)
        dataset.GetRasterBand(1).SetUnitType("m")
        dataset = None
    with pytest.raises(ValueError, match="Conflicting OPR band/vertical CRS units"):
        normalize_opr_tiles([tile], tmp_path)


def test_manifest_round_trip_preserves_selection_geometry_and_effective_grid(tmp_path):
    geometry = Polygon([(-84.12, 37.69), (-84.09, 37.69), (-84.12, 37.71)])
    manifest = Manifest(
        AOI(geometry=geometry),
        [_tile()],
        cellsize=1.5,
        opr_projects=[300169],
        output_crs="EPSG:32616",
        no_snap=True,
    )
    path = tmp_path / "manifest.json"
    manifest.save(path)
    restored = Manifest.load(path)
    assert restored.aoi.geometry.equals(geometry)
    assert restored.opr_projects == [300169]
    assert restored.cellsize == 1.5
    assert restored.no_snap
    assert restored.tiles[0].model_dump() == manifest.tiles[0].model_dump()


def test_mixed_pipeline_and_offline_replay_with_different_projection_and_resolution(tmp_path):
    opr_tile = _tile()
    values = np.full((10, 10), 100, dtype=np.float32)
    values[3:7, 3:7] = -9999
    opr_tile.local_path = _raster(
        tmp_path / "opr.tif", "EPSG:6473+6360", values, (5395000, 2, 0, 3785000, 0, -2)
    )
    to_utm = pyproj.Transformer.from_crs(6473, 32616, always_xy=True)
    x, y = to_utm.transform(5395010, 3784990)
    standard = _tile("standard_old", "1m", 2020)
    standard.local_path = _raster(
        tmp_path / "standard.tif",
        "EPSG:32616",
        np.full((30, 30), 10),
        (int(x) - 15, 1, 0, int(y) + 15, 0, -1),
    )
    to_wgs84 = pyproj.Transformer.from_crs(6473, 4326, always_xy=True)
    aoi = AOI(
        geometry=Polygon(
            [
                to_wgs84.transform(e, n)
                for e, n in [
                    (5395000, 3785000),
                    (5395020, 3785000),
                    (5395020, 3784980),
                    (5395000, 3784980),
                ]
            ]
        )
    )
    for tile in (standard, opr_tile):
        tile.bounds_wgs84 = aoi.bounds()
    bounds = ProjectBoundaries(gpd.GeoDataFrame(geometry=[aoi.geometry], crs=4326))
    output = tmp_path / "dem.tif"
    with (
        patch("src.demetrius.tnm.TNMTileSource") as source,
        patch.object(opr, "OPRTileSource") as opr_source,
        patch.object(ProjectBoundaries, "from_usgs", return_value=bounds),
        patch(
            "src.demetrius.downloader.TileDownloader.download", return_value=[opr_tile, standard]
        ),
    ):
        source.return_value.search.return_value = [standard]
        opr_source.return_value.search.return_value = [opr_tile]
        opr_source.return_value.footprints.return_value = bounds
        result = run_pipeline(
            aoi,
            output,
            output_crs="EPSG:32616",
            opr_projects=[300169],
            require_full_coverage=True,
            no_overviews=True,
        )
    assert result.status == "success", result.error
    dataset = gdal.Open(str(output))
    result_values = dataset.ReadAsArray()
    assert np.any(np.isclose(result_values, 30.48006096))
    assert np.any(result_values == 10)
    original = result_values.copy()
    dataset = None
    saved = Manifest.load(result.manifest_path)
    assert saved.tiles[1].local_path == opr_tile.local_path
    with (
        patch("src.demetrius.tnm.TNMTileSource", side_effect=AssertionError("network")),
        patch.object(opr, "OPRTileSource", side_effect=AssertionError("network")),
    ):
        replay = run_pipeline(aoi, output, mode="process-only")
    assert replay.status == "success", replay.error
    np.testing.assert_array_equal(gdal.Open(str(output)).ReadAsArray(), original)


def test_opr_only_coverage_is_not_filtered_by_1m_footprints(tmp_path):
    tile = _tile()
    aoi = AOI(geometry=tile.bounds_wgs84.to_polygon())
    bounds = ProjectBoundaries(gpd.GeoDataFrame(geometry=[aoi.geometry], crs=4326))
    with (
        patch("src.demetrius.tnm.TNMTileSource") as source,
        patch.object(opr, "OPRTileSource") as opr_source,
        patch.object(ProjectBoundaries, "from_usgs", side_effect=AssertionError("1m footprints")),
        patch("src.demetrius.downloader.TileDownloader.download", return_value=[tile]),
    ):
        source.return_value.search.return_value = []
        opr_source.return_value.search.return_value = [tile]
        opr_source.return_value.footprints.return_value = bounds
        result = run_pipeline(
            aoi,
            tmp_path / "dem.tif",
            opr_projects=[300169],
            mode="download-only",
            require_full_coverage=True,
        )
    assert result.status == "success", result.error
    assert Manifest.load(result.manifest_path).opr_projects == [300169]


@pytest.mark.parametrize("command", ["process", "batch"])
def test_cli_passes_repeatable_project_ids(tmp_path, command):
    aoi = tmp_path / "aoi.geojson"
    aoi.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    args = (
        ["--aoi", str(aoi)]
        if command == "process"
        else [
            "--input",
            str(aoi),
            "--name-field",
            "name",
        ]
    )
    target = (
        "src.demetrius.pipeline.run_pipeline"
        if command == "process"
        else "src.demetrius.batch.batch_process"
    )
    result = PipelineResult(name="dem", status="success")
    with patch(target, return_value=result if command == "process" else [result]) as run:
        response = CliRunner().invoke(
            cli, [command, *args, "--opr-project", "300169", "--opr-project", "2730"]
        )
    assert response.exit_code == 0, response.output
    assert run.call_args.kwargs["opr_projects"] == (300169, 2730)


def test_inspect_includes_opr_outside_standard_footprints(tmp_path):
    tile = _tile()
    aoi = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame(geometry=[tile.bounds_wgs84.to_polygon()], crs=4326).to_file(aoi)
    bounds = ProjectBoundaries(
        gpd.GeoDataFrame(geometry=[tile.bounds_wgs84.to_polygon()], crs=4326)
    )
    with (
        patch("src.demetrius.cli.TNMTileSource") as source,
        patch.object(opr, "OPRTileSource") as opr_source,
        patch.object(ProjectBoundaries, "from_usgs", side_effect=AssertionError("1m coverage")),
    ):
        source.return_value.search.return_value = []
        opr_source.return_value.search.return_value = [tile]
        opr_source.return_value.footprints.return_value = bounds
        response = CliRunner().invoke(
            cli, ["inspect", "--aoi", str(aoi), "--opr-project", "300169"]
        )
    assert response.exit_code == 0, response.output
    assert "KY_CentralEast_2_A23" in response.output
    assert "Source resolution (m): 0.609601219202438" in response.output
    assert "Source CRS: EPSG:6473" in response.output
    assert "LiDAR flight dates: 2023-01-01" in response.output


def test_opr_footprints_are_limited_to_resolved_tiles(monkeypatch):
    monkeypatch.setattr(opr, "query_projects", lambda **kwargs: [_feature()])
    source = opr.OPRTileSource([300169])
    tile = _tile()
    footprints = source.footprints([tile])
    assert footprints.gdf.geometry.union_all().equals(tile.bounds_wgs84.to_polygon())
    assert not footprints.intersects_coverage(box(-84.9, 37.1, -84.8, 37.2))
    source.client.close()
