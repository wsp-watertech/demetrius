"""Unit tests for core demetrius logic."""

import pytest
from datetime import datetime
from pathlib import Path

from src.demetrius.models import Tile, BoundingBox, AOI
from src.demetrius.parser import parse_dataset_and_tile_ids
from src.demetrius.priority import prioritize_datasets
from src.demetrius.filtering import filter_tiles_by_aoi, get_coverage_polygon
from src.demetrius.coverage import validate_coverage
from src.demetrius.crs import detect_utm_zones_from_tiles, get_target_utm_for_tiles
from shapely.geometry import box


class TestDatasetParser:
    """Test dataset and tile ID extraction."""

    def test_parse_standard_tnm_title(self):
        """Test parsing standard TNM title format."""
        title = "USGS 1 Meter 18 x38y448 PA_3_County_South_Central_2018_D18"
        dataset_id, tile_id = parse_dataset_and_tile_ids(title)
        assert dataset_id == "PA_3_County_South_Central_2018_D18"
        assert tile_id == "x38y448"

    def test_parse_alternate_title(self):
        """Test parsing alternate title format."""
        title = "USGS 1 Meter 1 x10y20 SomeState_County_2020_D20"
        dataset_id, tile_id = parse_dataset_and_tile_ids(title)
        assert dataset_id == "SomeState_County_2020_D20"
        assert tile_id == "x10y20"

    def test_parse_missing_tile_id(self):
        """Test error on missing tile ID."""
        with pytest.raises(ValueError, match="Could not extract tile_id"):
            parse_dataset_and_tile_ids("Invalid Title Without Coordinates")


class TestTileModels:
    """Test core tile models."""

    def test_bounding_box_creation(self):
        """Test BoundingBox creation and conversion."""
        bbox = BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1)
        assert bbox.as_tuple() == (-74.5, 40.0, -74.4, 40.1)
        polygon = bbox.to_polygon()
        assert polygon.area > 0

    def test_bounding_box_validation(self):
        """Test BoundingBox validation."""
        with pytest.raises(ValueError, match="Longitude"):
            BoundingBox(min_x=190, min_y=40.0, max_x=-74.4, max_y=40.1)

    def test_tile_creation(self):
        """Test Tile creation."""
        bbox = BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1)
        tile = Tile(
            id="test_tile",
            dataset_id="PA_3_County_2018",
            tile_id="x38y448",
            publication_date=datetime(2021, 11, 18),
            last_updated=datetime(2021, 11, 22),
            download_url="https://example.com/tile.tif",
            bounds_wgs84=bbox,
            priority=0,
        )
        assert tile.dataset_id == "PA_3_County_2018"
        assert tile.priority == 0


class TestPrioritization:
    """Test dataset prioritization logic."""

    def test_prioritize_single_dataset(self):
        """Test prioritization with single dataset."""
        tiles = [
            Tile(
                id=f"tile_{i}",
                dataset_id="PA_County_2018",
                tile_id=f"x{i}y{i}",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="http://example.com/tile.tif",
                bounds_wgs84=BoundingBox(
                    min_x=-74.5 + i * 0.1,
                    min_y=40.0,
                    max_x=-74.4 + i * 0.1,
                    max_y=40.1,
                ),
                priority=-1,
            )
            for i in range(3)
        ]

        result = prioritize_datasets(tiles)
        assert len(result) == 3
        assert all(t.priority == 0 for t in result)

    def test_prioritize_multiple_datasets(self):
        """Test prioritization with multiple datasets."""
        tiles = [
            Tile(
                id="tile_old",
                dataset_id="PA_County_2018",
                tile_id="x0y0",
                publication_date=datetime(2020, 1, 1),
                last_updated=datetime(2020, 1, 1),
                download_url="http://example.com/tile1.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=-1,
            ),
            Tile(
                id="tile_new",
                dataset_id="PA_County_2021",
                tile_id="x0y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="http://example.com/tile2.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=-1,
            ),
        ]

        result = prioritize_datasets(tiles)
        # Newer dataset should have priority 0, older should be 1 (or skipped as duplicate)
        assert len(result) <= 2
        priorities = [t.priority for t in result]
        assert 0 in priorities


class TestFiltering:
    """Test tile filtering logic."""

    def test_filter_by_aoi_intersection(self):
        """Test filtering tiles by AOI intersection."""
        # Create tiles
        tiles = [
            Tile(
                id="tile_1",
                dataset_id="PA_County",
                tile_id="x0y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="http://example.com/tile1.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=0,
            ),
            Tile(
                id="tile_2",
                dataset_id="PA_County",
                tile_id="x1y1",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="http://example.com/tile2.tif",
                bounds_wgs84=BoundingBox(min_x=100.0, min_y=20.0, max_x=100.1, max_y=20.1),
                priority=0,
            ),
        ]

        # Create AOI that intersects only tile_1
        aoi = AOI(geometry=box(-74.45, 40.05, -74.41, 40.09), buffer_distance=0)

        filtered = filter_tiles_by_aoi(tiles, aoi)
        assert len(filtered) == 1
        assert filtered[0].id == "tile_1"

    def test_coverage_polygon(self):
        """Test coverage polygon generation."""
        tiles = [
            Tile(
                id=f"tile_{i}",
                dataset_id="PA_County",
                tile_id=f"x{i}y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="http://example.com/tile.tif",
                bounds_wgs84=BoundingBox(
                    min_x=-74.5 + i * 0.1,
                    min_y=40.0,
                    max_x=-74.4 + i * 0.1,
                    max_y=40.1,
                ),
                priority=0,
            )
            for i in range(3)
        ]

        coverage = get_coverage_polygon(tiles)
        assert coverage.area > 0


class TestCoverageValidation:
    """Test coverage validation."""

    def test_full_coverage(self):
        """Test validation with full coverage."""
        # Create AOI
        aoi = AOI(geometry=box(-74.5, 40.0, -74.3, 40.2), buffer_distance=0)

        # Create tiles that fully cover AOI
        tiles = [
            Tile(
                id="tile_1",
                dataset_id="PA_County",
                tile_id="x0y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="http://example.com/tile.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.3, max_y=40.2),
                priority=0,
            ),
        ]

        report = validate_coverage(tiles, aoi, require_full_coverage=False)
        assert report["is_complete"]


class TestCRS:
    """Test CRS handling."""

    def test_detect_utm_zones(self):
        """Test UTM zone detection."""
        tiles = [
            Tile(
                id="tile_1",
                dataset_id="PA_County",
                tile_id="x0y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="http://example.com/tile.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=0,
            ),
        ]

        zones = detect_utm_zones_from_tiles(tiles)
        assert len(zones) > 0
        assert all(1 <= z <= 60 for z in zones)

    def test_target_utm_selection(self):
        """Test target UTM zone selection."""
        tiles = [
            Tile(
                id="tile_1",
                dataset_id="PA_County",
                tile_id="x0y0",
                publication_date=datetime(2021, 11, 18),
                last_updated=datetime(2021, 11, 22),
                download_url="http://example.com/tile.tif",
                bounds_wgs84=BoundingBox(min_x=-74.5, min_y=40.0, max_x=-74.4, max_y=40.1),
                priority=0,
            ),
        ]

        crs = get_target_utm_for_tiles(tiles)
        assert crs.startswith("EPSG:")
