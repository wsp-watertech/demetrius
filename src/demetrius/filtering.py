"""Tile filtering and selection logic."""

import logging
from typing import Optional, Sequence

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .models import AOI, Tile
from .project_boundaries import ProjectBoundaries

logger = logging.getLogger(__name__)


def filter_tiles_by_aoi(
    tiles: Sequence[Tile],
    aoi: AOI,
    project_bounds: Optional[ProjectBoundaries] = None,
) -> list[Tile]:
    """Filter tiles to only those intersecting the buffered AOI with actual coverage.

    If project_bounds is provided, only keeps tiles that intersect actual project
    coverage areas (filling coverage gaps). Otherwise, uses simple bbox intersection.

    Args:
        tiles: Raw tiles from TNM
        aoi: Area of Interest with buffer
        project_bounds: Optional project boundaries for coverage-aware filtering

    Returns:
        List of tiles that intersect buffered AOI and have coverage (if project_bounds provided)
    """
    logger.info(f"Filtering {len(tiles)} tiles by AOI intersection")

    # Get buffered AOI as polygon
    buffered_aoi = aoi.buffered_geometry()

    filtered = []
    for tile in tiles:
        tile_polygon = tile.bounds_wgs84.to_polygon()

        # Check basic intersection
        if not tile_polygon.intersects(buffered_aoi):
            logger.debug(f"Discarded tile {tile.tile_id} (outside buffered AOI)")
            continue

        # If project boundaries provided, check for actual coverage
        if project_bounds is not None:
            if not project_bounds.intersects_coverage(tile_polygon):
                logger.debug(
                    f"Discarded tile {tile.tile_id} (no project coverage in tile area)"
                )
                continue
            logger.debug(f"Kept tile {tile.tile_id} (intersects buffered AOI and coverage)")
        else:
            logger.debug(f"Kept tile {tile.tile_id} (intersects buffered AOI)")

        filtered.append(tile)

    logger.info(f"Filtered to {len(filtered)} tiles")
    return filtered


def validate_bounds_compatibility(tiles: Sequence[Tile]) -> None:
    """Validate that all tiles have valid bounding boxes.

    Args:
        tiles: Tiles to validate

    Raises:
        ValueError: If tiles have invalid bounds
    """
    for tile in tiles:
        bounds = tile.bounds_wgs84
        if bounds.min_x >= bounds.max_x or bounds.min_y >= bounds.max_y:
            raise ValueError(f"Invalid bounds for tile {tile.id}: {bounds}")


def get_coverage_polygon(tiles: Sequence[Tile]) -> Polygon:
    """Get union of all tile extents.

    Args:
        tiles: Tiles to union

    Returns:
        Shapely Polygon of combined coverage
    """
    if not tiles:
        return Polygon()

    polygons = [tile.bounds_wgs84.to_polygon() for tile in tiles]
    
    # Use cascaded union to handle multiple polygons
    from shapely.ops import unary_union
    return unary_union(polygons)
