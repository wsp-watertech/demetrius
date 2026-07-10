"""Tile filtering and selection logic."""

import logging
from typing import Sequence

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .models import AOI, Tile

logger = logging.getLogger(__name__)


def filter_tiles_by_aoi(tiles: Sequence[Tile], aoi: AOI) -> list[Tile]:
    """Filter tiles to only those intersecting the buffered AOI.

    Args:
        tiles: Raw tiles from TNM
        aoi: Area of Interest with buffer

    Returns:
        List of tiles that intersect buffered AOI
    """
    logger.info(f"Filtering {len(tiles)} tiles by AOI intersection")

    # Get buffered AOI as polygon
    buffered_aoi = aoi.geometry.buffer(aoi.buffer_distance)

    filtered = []
    for tile in tiles:
        tile_polygon = tile.bounds_wgs84.to_polygon()

        # Check intersection
        if tile_polygon.intersects(buffered_aoi):
            filtered.append(tile)
            logger.debug(f"Kept tile {tile.tile_id} (intersects buffered AOI)")
        else:
            logger.debug(f"Discarded tile {tile.tile_id} (outside buffered AOI)")

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
