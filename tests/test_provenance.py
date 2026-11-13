"""Project tracking must follow actual warped elevation validity and precedence."""

import json
import subprocess
import sys
from datetime import datetime
from unittest.mock import patch
from xml.etree import ElementTree as ET

import numpy as np
import pyproj
import pytest
from click.testing import CliRunner
from osgeo import gdal
from shapely.geometry import Polygon

from src.demetrius.cli import cli
from src.demetrius.elevation_converter import ElevationConverter
from src.demetrius.manifest import Manifest
from src.demetrius.merger import DatasetMerger
from src.demetrius.models import AOI, BoundingBox, Tile
from src.demetrius.pipeline import PipelineResult, run_pipeline
from src.demetrius.projections import get_projection_file
from src.demetrius.provenance import elevation_band, project_categories, write_projects


def _tile(
    path,
    name,
    priority,
    values,
    *,
    nodata=-9999,
    mask=None,
    crs="EPSG:32618",
    transform=(500000, 1, 0, 4500000, 0, -1),
):
    with gdal.ExceptionMgr():
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(path), values.shape[1], values.shape[0], 1, gdal.GDT_Float32
        )
        dataset.SetProjection(pyproj.CRS(crs).to_wkt())
        dataset.SetGeoTransform(transform)
        band = dataset.GetRasterBand(1)
        if nodata is not None:
            band.SetNoDataValue(nodata)
        band.WriteArray(values.astype(np.float32))
        if mask is not None:
            band.CreateMaskBand(gdal.GMF_PER_DATASET)
            band.GetMaskBand().WriteArray(mask.astype(np.uint8) * 255)
        dataset.Close()
    return Tile(
        id=name,
        dataset_id=name,
        tile_id="tile",
        priority=priority,
        publication_date=datetime.fromisoformat("2024-01-01"),
        last_updated=datetime.fromisoformat("2024-01-01"),
        download_url="https://example.com/tile.tif",
        bounds_wgs84=BoundingBox(min_x=-75, min_y=40, max_x=-74, max_y=41),
        local_path=str(path),
    )


def _assert_ids_only_matches(merger, tiles, expected, output, **options):
    actual = merger.merge_reproject_clip(
        tiles, output, sources_only=True, **{"target_crs": "EPSG:32618", "cellsize": 1, **options}
    )
    with gdal.ExceptionMgr():
        reference = gdal.Open(str(expected))
        dataset = gdal.Open(str(actual))
        assert dataset.RasterCount == 1
        assert dataset.GetRasterBand(1).DataType == gdal.GDT_Int32
        assert dataset.GetRasterBand(1).GetNoDataValue() == -1
        assert dataset.GetGeoTransform() == reference.GetGeoTransform()
        assert dataset.GetProjection() == reference.GetProjection()
        np.testing.assert_array_equal(dataset.ReadAsArray(), reference.ReadAsArray())


@pytest.mark.parametrize(
    "nodata,explicit_mask",
    [(-9999, False), (0, False), (float("nan"), False), (None, True), (-9999, True), (0, True)],
)
def test_priorities_follow_valid_elevations_including_explicit_masks(
    tmp_path, nodata, explicit_mask
):
    old_values = np.full((12, 12), 10)
    old_values[5, 5] = -1
    old_values[0:2, 0:2] = -9999
    new_values = np.full((12, 12), 20, dtype=np.float32)
    explicit = np.ones((12, 12), dtype=bool)
    if nodata is None:
        explicit[4:8, 4:8] = False
    else:
        new_values[4:8, 4:8] = nodata
        new_values[:2, :2] = nodata
    explicit[:2, :2] = False
    explicit[8:10, 8:10] = False
    tiles = [
        _tile(tmp_path / "old.tif", "old", 0, old_values),
        _tile(
            tmp_path / "new.tif",
            "new",
            3,
            new_values,
            nodata=nodata,
            mask=explicit if explicit_mask else None,
        ),
    ]
    merger = DatasetMerger(tmp_path)
    plain = merger.merge_reproject_clip(tiles, tmp_path / "plain.tif", "EPSG:32618", cellsize=1)
    merged = merger.merge_reproject_clip(
        tiles, tmp_path / "merged.tif", "EPSG:32618", cellsize=1, track_projects=True
    )
    dem = elevation_band(merged, tmp_path / "dem.vrt")
    output = write_projects(
        merged, dem, tmp_path / "dem.sources.tif", project_categories(tiles), overviews=False
    )
    with gdal.ExceptionMgr():
        elevations = gdal.Open(str(dem)).ReadAsArray()
        dataset = gdal.Open(str(output))
        values = dataset.ReadAsArray()
        baseline = gdal.Open(str(plain))
        baseline_valid = baseline.GetRasterBand(1).GetMaskBand().ReadAsArray() != 0
        np.testing.assert_array_equal(baseline_valid, np.isfinite(elevations))
        # The untracked GDAL path can ignore explicit masks when nodata is set.
        if not (explicit_mask and nodata is not None):
            np.testing.assert_array_equal(
                baseline.ReadAsArray()[baseline_valid], elevations[baseline_valid]
            )
        assert np.all(values[elevations == 10] == 0)
        assert np.all(values[elevations == -1] == 0)
        assert np.all(values[elevations == 20] == 3)
        assert np.all(values[np.isnan(elevations)] == -1)
        assert set(np.unique(values)) == {-1, 0, 3}
        assert dataset.GetRasterBand(1).DataType == gdal.GDT_Int32
        assert dataset.GetRasterBand(1).GetNoDataValue() == -1
        rat = dataset.GetRasterBand(1).GetDefaultRAT()
        assert [rat.GetValueAsInt(i, 0) for i in range(2)] == [0, 3]
        assert [rat.GetValueAsString(i, 1) for i in range(2)] == ["old", "new"]
        assert dataset.GetRasterBand(1).GetCategoryNames() == ["old", "", "", "new"]
    style = ET.parse(output.with_suffix(".qml"))
    assert [node.get("value") for node in style.findall(".//paletteEntry")] == ["0", "3"]
    assert len({node.get("color") for node in style.findall(".//paletteEntry")}) == 2
    _assert_ids_only_matches(merger, tiles, output, tmp_path / "ids-only.tif")


@pytest.mark.parametrize("clip,snap", [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize("resampling", ["bilinear", "cubic", "cubicspline", "lanczos"])
def test_tracking_preserves_resampled_dem_and_exact_grid(tmp_path, clip, snap, resampling):
    surface = np.arange(256, dtype=np.float32).reshape(16, 16)
    new = surface + 1000
    new[5:10, 5:10] = -9999
    tiles = [
        _tile(tmp_path / "old.tif", "old", 0, surface + 10),
        _tile(tmp_path / "new.tif", "new", 1, new),
    ]
    transformer = pyproj.Transformer.from_crs(32618, 4326, always_xy=True)
    cutline = (
        Polygon(
            [
                transformer.transform(x, y)
                for x, y in [(500001, 4499999), (500014, 4499999), (500003, 4499986)]
            ]
        )
        if clip
        else None
    )
    merger = DatasetMerger(tmp_path)
    options = {
        "target_crs": "EPSG:32618",
        "cellsize": 0.75,
        "cutline_geometry": cutline,
        "snap_to_grid": snap,
        "resampling": resampling,
    }
    plain = merger.merge_reproject_clip(tiles, tmp_path / "plain.tif", **options)
    tracked = merger.merge_reproject_clip(
        tiles, tmp_path / "tracked.tif", **options, track_projects=True
    )
    dem = elevation_band(tracked, tmp_path / "dem.vrt")
    write_projects(
        tracked, dem, tmp_path / "dem.sources.tif", project_categories(tiles), overviews=False
    )
    with gdal.ExceptionMgr():
        baseline = gdal.Open(str(plain))
        tracked_dem = gdal.Open(str(dem))
        projects = gdal.Open(str(tmp_path / "dem.sources.tif"))
        valid = baseline.GetRasterBand(1).GetMaskBand().ReadAsArray() != 0
        np.testing.assert_array_equal(
            valid, tracked_dem.GetRasterBand(1).GetMaskBand().ReadAsArray() != 0
        )
        np.testing.assert_array_equal(
            baseline.ReadAsArray()[valid], tracked_dem.ReadAsArray()[valid]
        )
        assert baseline.GetGeoTransform() == projects.GetGeoTransform()
        assert baseline.GetProjection() == projects.GetProjection()
        assert (baseline.RasterXSize, baseline.RasterYSize) == (
            projects.RasterXSize,
            projects.RasterYSize,
        )
        assert np.all(projects.ReadAsArray()[~valid] == -1)
    _assert_ids_only_matches(
        merger, tiles, tmp_path / "dem.sources.tif", tmp_path / "ids-only.tif", **options
    )


def test_same_dataset_multiple_crss_has_one_category(tmp_path):
    one = _tile(tmp_path / "one.tif", "same", 2, np.ones((4, 4)))
    two = _tile(tmp_path / "two.tif", "same", 2, np.ones((4, 4)) * 5, crs="EPSG:26918")
    categories = project_categories([one, two])
    assert len(categories) == 1 and categories[0]["value"] == 2
    merged = DatasetMerger(tmp_path).merge_reproject_clip(
        [one, two], tmp_path / "merged.tif", "EPSG:32618", cellsize=1, track_projects=True
    )
    dem = elevation_band(merged, tmp_path / "dem.vrt")
    output = write_projects(merged, dem, tmp_path / "dem.sources.tif", categories, overviews=False)
    elevations = gdal.Open(str(dem))
    valid = elevations.GetRasterBand(1).GetMaskBand().ReadAsArray() != 0
    assert np.all(gdal.Open(str(output)).ReadAsArray()[valid] == 2)
    _assert_ids_only_matches(DatasetMerger(tmp_path), [one, two], output, tmp_path / "ids-only.tif")


@pytest.mark.parametrize("resampling", ["bilinear", "cubic", "cubicspline", "lanczos"])
@pytest.mark.parametrize(
    "target,cellsize,snap", [("EPSG:5070", 0.75, False), ("ffrd", 4, True), ("ffrd", 8, False)]
)
def test_ids_only_matches_reprojected_mixed_resolution_masks(
    tmp_path, resampling, target, cellsize, snap
):
    rng = np.random.default_rng(42)
    old = np.where(rng.random((64, 64)) < 0.2, -9999, 10)
    new = np.where(rng.random((32, 32)) < 0.4, -9999, 20)
    tiles = [
        _tile(tmp_path / "old.tif", "old", 0, old),
        _tile(
            tmp_path / "new.tif",
            "new",
            17,
            new,
            crs="EPSG:26918",
            transform=(500003.3, 1.5, 0, 4499998.7, 0, -1.5),
        ),
    ]
    target_crs = get_projection_file("ffrd.prj").read_text() if target == "ffrd" else target
    options = {
        "target_crs": target_crs,
        "cellsize": cellsize,
        "snap_to_grid": snap,
        "resampling": resampling,
    }
    merger = DatasetMerger(tmp_path)
    tracked = merger.merge_reproject_clip(
        tiles, tmp_path / "tracked.tif", track_projects=True, **options
    )
    dem = elevation_band(tracked, tmp_path / "dem.vrt")
    paired = write_projects(
        tracked, dem, tmp_path / "paired.sources.tif", project_categories(tiles), overviews=False
    )
    _assert_ids_only_matches(merger, tiles, paired, tmp_path / "ids-only.tif", **options)


def test_lanczos_normalizes_constant_ids_without_changing_elevations(tmp_path, monkeypatch):
    tile = _tile(tmp_path / "tile.tif", "project", 17, np.full((8, 8), 20))
    original_warp = DatasetMerger._run_combined_gdalwarp

    def warp_with_near_unity_weights(self, *args, **kwargs):
        output = original_warp(self, *args, **kwargs)
        # GDAL 3.8 skips Lanczos normalization for weight sums within 1e-5
        # of unity. Reproduce this independently of local GDAL/PROJ versions.
        with gdal.ExceptionMgr():
            dataset = gdal.Open(str(output), gdal.GA_Update)
            for number in range(1, dataset.RasterCount + 1):
                band = dataset.GetRasterBand(number)
                if band.GetColorInterpretation() != gdal.GCI_AlphaBand:
                    band.WriteArray(band.ReadAsArray() * 0.9999928)
            band = None
            dataset.Close()
        return output

    monkeypatch.setattr(DatasetMerger, "_run_combined_gdalwarp", warp_with_near_unity_weights)
    tracked = DatasetMerger(tmp_path).merge_reproject_clip(
        [tile],
        tmp_path / "tracked.tif",
        "EPSG:32618",
        cellsize=1,
        resampling="lanczos",
        track_projects=True,
    )
    dem = elevation_band(tracked, tmp_path / "dem.vrt")
    before = gdal.Open(str(dem)).ReadAsArray().copy()
    output = write_projects(
        tracked, dem, tmp_path / "dem.sources.tif", project_categories([tile]), overviews=False
    )
    assert np.all(gdal.Open(str(output)).ReadAsArray() == 17)
    np.testing.assert_array_equal(gdal.Open(str(dem)).ReadAsArray(), before)
    np.testing.assert_allclose(before, 20 * 0.9999928, rtol=1e-7)


@pytest.mark.parametrize(
    "saved_no_sources,saved_sources_only,override",
    [
        (True, False, {}),
        (False, False, {}),
        (False, True, {}),
        (True, False, {"sources_only": True}),
        (False, True, {"no_sources": True}),
    ],
)
def test_pipeline_sources_options_persist_for_offline_replay(
    tmp_path, saved_no_sources, saved_sources_only, override
):
    tile = _tile(tmp_path / "tile.tif", "old", 0, np.full((8, 8), 10))
    aoi = AOI(geometry=tile.bounds_wgs84.to_polygon())
    output = tmp_path / "dem.tif"
    Manifest(
        aoi,
        [tile],
        cellsize=1,
        output_crs="EPSG:32618",
        no_clip=True,
        no_sources=saved_no_sources,
        sources_only=saved_sources_only,
    ).save(tmp_path / "dem.tif.manifest.json")
    no_sources = override.get("no_sources", False) if override else saved_no_sources
    sources_only = override.get("sources_only", False) if override else saved_sources_only
    with (
        patch("src.demetrius.tnm.TNMTileSource", side_effect=AssertionError("network")),
        patch(
            "src.demetrius.elevation_converter.ElevationConverter.convert_and_generate_cog",
            wraps=ElevationConverter().convert_and_generate_cog,
        ) as converter,
        patch("subprocess.run", wraps=subprocess.run) as commands,
    ):
        result = run_pipeline(aoi, output, mode="process-only", **override)
    assert result.status == "success", result.error
    if no_sources:
        assert result.sources_path is None
        assert not (tmp_path / "dem.sources.tif").exists()
    else:
        assert result.sources_path == tmp_path / "dem.sources.tif"
        assert np.all(gdal.Open(str(result.sources_path)).ReadAsArray() == 0)
    if sources_only:
        converter.assert_not_called()
        assert result.output_path is None
        assert not output.exists()
        warps = [call.args[0] for call in commands.call_args_list if call.args[0][0] == "gdalwarp"]
        assert len(warps) == 1
        assert warps[0][-1] == str(result.sources_path)
        assert "-novshift" in warps[0]
        assert not any(
            "merged.tif" in arg for cmd in commands.call_args_list for arg in cmd.args[0]
        )
    else:
        converter.assert_called_once()
        assert gdal.Open(str(output)).RasterCount == 1


def test_legacy_manifest_no_provenance_setting_is_migrated(tmp_path):
    aoi = AOI(geometry=BoundingBox(min_x=-75, min_y=40, max_x=-74, max_y=41).to_polygon())
    data = Manifest(aoi, [], output_crs="EPSG:32618").to_dict()
    data["processing"].pop("no_sources")
    data["processing"].pop("sources_only")
    data["processing"]["no_provenance"] = True
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    restored = Manifest.load(path)
    assert restored.no_sources
    assert not restored.sources_only
    assert "no_provenance" not in restored.to_dict()["processing"]


@pytest.mark.parametrize("command", ["process", "batch"])
def test_cli_source_flags_forward_and_conflict(tmp_path, command):
    input_path = tmp_path / "aoi.geojson"
    input_path.write_text("{}")
    result = PipelineResult(name="aoi", status="success", sources_path=tmp_path / "dem.sources.tif")
    if command == "process":
        args = ["process", "--aoi", str(input_path), "--output", str(tmp_path / "dem.tif")]
        target = "src.demetrius.pipeline.run_pipeline"
        returned = result
    else:
        args = ["batch", "--input", str(input_path), "--name-field", "name"]
        target = "src.demetrius.batch.batch_process"
        returned = [result]
    with patch(target, return_value=returned) as runner:
        invoked = CliRunner().invoke(cli, [*args, "--sources-only"])
        assert invoked.exit_code == 0, invoked.output
        assert runner.call_args.kwargs["sources_only"] is True
        assert runner.call_args.kwargs["no_sources"] is False
        assert "dem.sources.tif" in invoked.output
        assert "DEM saved to:" not in invoked.output
        assert "None" not in invoked.output
    with patch(target) as runner:
        invoked = CliRunner().invoke(cli, [*args, "--sources-only", "--no-sources"])
        assert invoked.exit_code == 2
        assert "mutually exclusive" in invoked.output
        runner.assert_not_called()


def test_pipeline_rejects_conflicting_sources_flags(tmp_path):
    result = run_pipeline(
        tmp_path / "missing.geojson", tmp_path / "dem.tif", no_sources=True, sources_only=True
    )
    assert result.status == "failed"
    assert "mutually exclusive" in result.error


def test_categorical_overviews_preserve_only_priorities(tmp_path):
    old = _tile(tmp_path / "old.tif", "old", 0, np.full((512, 512), 10))
    new_values = np.full((512, 512), 20)
    new_values[:, :257] = -9999
    new = _tile(tmp_path / "new.tif", "new", 9, new_values)
    merged = DatasetMerger(tmp_path).merge_reproject_clip(
        [old, new], tmp_path / "merged.tif", "EPSG:32618", cellsize=1, track_projects=True
    )
    dem = elevation_band(merged, tmp_path / "dem.vrt")
    output = write_projects(
        merged, dem, tmp_path / "dem.sources.tif", project_categories([old, new]), overviews=True
    )
    dataset = gdal.Open(str(output))
    band = dataset.GetRasterBand(1)
    assert band.GetOverviewCount() == 1
    assert set(np.unique(band.GetOverview(0).ReadAsArray())) == {0, 9}
    persisted = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                "-c",
                (
                    "import json, sys; from osgeo import gdal; "
                    "d = gdal.Open(sys.argv[1]); b = d.GetRasterBand(1); r = b.GetDefaultRAT(); "
                    "print(json.dumps([b.GetCategoryNames(), "
                    "[[r.GetValueAsInt(i, 0), r.GetValueAsString(i, 1), "
                    "[r.GetValueAsInt(i, c) for c in range(2, 6)]] "
                    "for i in range(r.GetRowCount())]]))"
                ),
                str(output),
            ],
            text=True,
        )
    )
    categories = project_categories([old, new])
    assert persisted[0][0] == "old" and persisted[0][9] == "new"
    assert persisted[1] == [[c["value"], c["name"], c["color"]] for c in categories]


@pytest.mark.parametrize("invalid", [float("nan"), 0.5, 42])
def test_invalid_attribution_is_an_explicit_error(tmp_path, invalid):
    tile = _tile(tmp_path / "tile.tif", "old", 0, np.full((4, 4), 10))
    merged = DatasetMerger(tmp_path).merge_reproject_clip(
        [tile], tmp_path / "merged.tif", "EPSG:32618", cellsize=1, track_projects=True
    )
    with gdal.ExceptionMgr():
        dataset = gdal.Open(str(merged), gdal.GA_Update)
        dataset.GetRasterBand(2).Fill(invalid)
        dataset.Close()
    dem = elevation_band(merged, tmp_path / "dem.vrt")
    with pytest.raises(ValueError, match="invalid project attribution"):
        write_projects(
            merged, dem, tmp_path / "dem.sources.tif", project_categories([tile]), overviews=False
        )


@pytest.mark.parametrize("command", ["process", "batch"])
def test_cli_exposes_provenance_opt_out(command):
    result = CliRunner().invoke(cli, [command, "--help"])
    assert result.exit_code == 0
    assert "--no-sources" in result.output
    assert "--sources-only" in result.output


@pytest.mark.parametrize("crs", ["EPSG:32618+5703", "EPSG:32618+6360"])
def test_vertical_transforms_do_not_change_priorities_or_elevations(tmp_path, crs):
    tile = _tile(tmp_path / "tile.tif", "old", 3, np.full((8, 8), 10), crs=crs)
    merger = DatasetMerger(tmp_path)
    baseline = merger.merge_reproject_clip([tile], tmp_path / "plain.tif", "EPSG:32618", cellsize=1)
    tracked = merger.merge_reproject_clip(
        [tile], tmp_path / "tracked.tif", "EPSG:32618", cellsize=1, track_projects=True
    )
    dem = elevation_band(tracked, tmp_path / "dem.vrt")
    output = write_projects(
        tracked, dem, tmp_path / "dem.sources.tif", project_categories([tile]), overviews=False
    )
    np.testing.assert_allclose(
        gdal.Open(str(dem)).ReadAsArray(),
        gdal.Open(str(baseline)).ReadAsArray(),
        atol=1e-5,
        rtol=0,
    )
    assert np.all(gdal.Open(str(output)).ReadAsArray() == 3)
    _assert_ids_only_matches(merger, [tile], output, tmp_path / "ids-only.tif")
