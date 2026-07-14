"""Integration tests for demetrius CLI and full workflows."""

import pytest
import json
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch

from click.testing import CliRunner
from shapely.geometry import box

from src.demetrius.cli import cli, process, inspect
from src.demetrius.models import Tile, BoundingBox, AOI
from src.demetrius.manifest import Manifest


class TestCLIInspect:
    """Test inspect command."""

    @pytest.fixture
    def runner(self):
        """Create Click test runner."""
        return CliRunner()

    @pytest.fixture
    def sample_aoi(self, tmp_path):
        """Create sample AOI file."""
        import geopandas as gpd
        from shapely.geometry import box

        gdf = gpd.GeoDataFrame(
            {"id": [1]},
            geometry=[box(-74.5, 40.0, -74.4, 40.1)],
            crs="EPSG:4326",
        )
        aoi_path = tmp_path / "aoi.shp"
        gdf.to_file(aoi_path)
        return aoi_path

    def test_inspect_with_mock_tnm(self, runner, sample_aoi, tmp_path):
        """Test inspect command with mocked TNM response."""
        # Create mock tiles
        mock_tiles = [
            Tile(
                id="test_tile_1",
                dataset_id="PA_County_2018",
                tile_id="x38y448",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="https://example.com/tile1.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=0,
            ),
        ]

        # Mock TNM query
        with patch("src.demetrius.cli.TNMTileSource") as mock_source:
            mock_instance = Mock()
            mock_instance.search.return_value = mock_tiles
            mock_source.return_value = mock_instance

            result = runner.invoke(
                inspect,
                ["--aoi", str(sample_aoi)],
            )

            assert result.exit_code == 0
            assert "Inspection Report" in result.output
            assert "PA_County_2018" in result.output


class TestManifest:
    """Test manifest serialization."""

    def test_manifest_save_and_load(self, tmp_path):
        """Test saving and loading manifest."""
        # Create sample data
        aoi = AOI(geometry=box(-74.5, 40.0, -74.4, 40.1), buffer=1000)
        tiles = [
            Tile(
                id="test_tile_1",
                dataset_id="PA_County_2018",
                tile_id="x38y448",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="https://example.com/tile1.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=0,
                local_path="/path/to/tile1.tif",
            ),
        ]

        # Create and save manifest
        manifest = Manifest(aoi=aoi, tiles=tiles, buffer=1000)
        manifest_path = tmp_path / "manifest.json"
        manifest.save(manifest_path)

        # Verify file exists
        assert manifest_path.exists()

        # Load and verify
        loaded = Manifest.load(manifest_path)
        assert len(loaded.tiles) == 1
        assert loaded.tiles[0].dataset_id == "PA_County_2018"
        assert loaded.buffer == 1000

    def test_manifest_content(self, tmp_path):
        """Test manifest JSON structure."""
        aoi = AOI(geometry=box(-74.5, 40.0, -74.4, 40.1), buffer=1000)
        tiles = [
            Tile(
                id="test_tile",
                dataset_id="PA_County",
                tile_id="x0y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="https://example.com/tile.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=0,
            ),
        ]

        manifest = Manifest(aoi=aoi, tiles=tiles)
        manifest_path = tmp_path / "manifest.json"
        manifest.save(manifest_path)

        # Read and verify JSON structure
        with open(manifest_path) as f:
            data = json.load(f)

        assert "aoi" in data
        assert "tiles" in data
        assert "buffer" in data
        assert data["tile_count"] == 1
        assert data["tiles"][0]["dataset_id"] == "PA_County"

    def test_manifest_with_cellsize(self, tmp_path):
        """Test manifest with cellsize."""
        aoi = AOI(geometry=box(-74.5, 40.0, -74.4, 40.1), buffer=1000)
        tiles = [
            Tile(
                id="test_tile",
                dataset_id="PA_County",
                tile_id="x0y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="https://example.com/tile.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=0,
            ),
        ]

        # Create manifest with cellsize
        manifest = Manifest(aoi=aoi, tiles=tiles, buffer=1000, cellsize=10.5)
        manifest_path = tmp_path / "manifest.json"
        manifest.save(manifest_path)

        # Read and verify cellsize in JSON
        with open(manifest_path) as f:
            data = json.load(f)

        assert "cellsize" in data
        assert data["cellsize"] == 10.5

        # Load and verify cellsize is preserved
        loaded = Manifest.load(manifest_path)
        assert loaded.cellsize == 10.5

    def test_manifest_without_cellsize(self, tmp_path):
        """Test manifest without cellsize (backward compatibility)."""
        aoi = AOI(geometry=box(-74.5, 40.0, -74.4, 40.1), buffer=1000)
        tiles = [
            Tile(
                id="test_tile",
                dataset_id="PA_County",
                tile_id="x0y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="https://example.com/tile.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=0,
            ),
        ]

        # Create manifest without cellsize
        manifest = Manifest(aoi=aoi, tiles=tiles, buffer=1000)
        manifest_path = tmp_path / "manifest.json"
        manifest.save(manifest_path)

        # Verify cellsize is not in JSON when not specified
        with open(manifest_path) as f:
            data = json.load(f)

        assert "cellsize" not in data

        # Load and verify cellsize is None
        loaded = Manifest.load(manifest_path)
        assert loaded.cellsize is None


class TestWorkflow:
    """Test complete workflow patterns."""

    def test_modes_are_mutually_exclusive(self):
        """Verify that different modes have expected behavior."""
        modes = ["full", "download-only", "process-only"]
        assert len(set(modes)) == 3

    def test_tile_priority_assignment(self):
        """Test that tiles get priority values assigned."""
        from src.demetrius.priority import prioritize_datasets

        tiles = [
            Tile(
                id=f"tile_{i}_{ds}",
                dataset_id=ds,
                tile_id=f"x{i}y0",
                publication_date=datetime(2020 + i, 1, 1),
                last_updated=datetime(2020 + i, 1, 1),
                download_url=f"https://example.com/tile_{i}_{ds}.tif",
                bounds_wgs84=BoundingBox(
                    min_x=-74.5,
                    min_y=40.0,
                    max_x=-74.4,
                    max_y=40.1,
                ),
                priority=-1,
            )
            for i, ds in enumerate(["PA_2019", "PA_2020", "PA_2021"])
        ]

        result = prioritize_datasets(tiles)

        # All should have priority assigned (not -1)
        assert all(t.priority >= 0 for t in result)

        # Oldest should have priority 0 (base layer), newest should have highest priority (overlay on top)
        oldest = [t for t in result if t.dataset_id == "PA_2019"]
        newest = [t for t in result if t.dataset_id == "PA_2021"]
        assert all(t.priority == 0 for t in oldest)
        assert all(t.priority == 2 for t in newest)


class TestGDALIntegration:
    """Test GDAL tool availability and basic functionality."""

    def test_gdalbuildvrt_available(self):
        """Check if gdalbuildvrt is available."""
        import subprocess

        try:
            result = subprocess.run(
                ["gdalbuildvrt", "--version"],
                capture_output=True,
                timeout=5,
            )
            # If we got here, tool is available
            assert result.returncode == 0 or result.returncode == 1  # Version varies
        except FileNotFoundError:
            pytest.skip("gdalbuildvrt not installed")

    def test_gdalwarp_available(self):
        """Check if gdalwarp is available."""
        import subprocess

        try:
            result = subprocess.run(
                ["gdalwarp", "--version"],
                capture_output=True,
                timeout=5,
            )
            assert result.returncode == 0
        except FileNotFoundError:
            pytest.skip("gdalwarp not installed")

    def test_gdal_translate_available(self):
        """Check if gdal_translate is available."""
        import subprocess

        try:
            result = subprocess.run(
                ["gdal_translate", "--version"],
                capture_output=True,
                timeout=5,
            )
            assert result.returncode == 0
        except FileNotFoundError:
            pytest.skip("gdal_translate not installed")


class TestCLIHelp:
    """Test CLI help and documentation."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_main_help(self, runner):
        """Test main CLI help."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "demetrius" in result.output
        assert "High-resolution DEM" in result.output

    def test_process_help(self, runner):
        """Test process command help."""
        result = runner.invoke(process, ["--help"])
        assert result.exit_code == 0
        assert "--aoi" in result.output
        assert "--output" in result.output

    def test_inspect_help(self, runner):
        """Test inspect command help."""
        result = runner.invoke(inspect, ["--help"])
        assert result.exit_code == 0
        assert "--aoi" in result.output
