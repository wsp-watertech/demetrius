"""Coverage validation and gap detection."""

import logging
from typing import Sequence

from shapely.geometry import Polygon

from .filtering import get_coverage_polygon
from .models import AOI, Tile

logger = logging.getLogger(__name__)


def validate_coverage(
    tiles: Sequence[Tile], aoi: AOI, require_full_coverage: bool = True
) -> dict[str, bool]:
    """Validate tile coverage of original AOI.

    Checks that the original (unbuffered) AOI is fully covered by
    selected tiles. Buffer gaps are allowed, but AOI gaps are not.

    Args:
        tiles: Selected tiles
        aoi: Area of Interest
        require_full_coverage: If True, raise error on gaps; if False, just warn

    Returns:
        Coverage report with 'is_complete' boolean

    Raises:
        ValueError: If require_full_coverage=True and gaps exist
    """
    logger.info(f"Validating coverage for {len(tiles)} tiles")

    # Get original AOI polygon (not buffered)
    original_aoi = aoi.geometry

    # Get union of tile extents
    tile_coverage = get_coverage_polygon(tiles)

    # Check if AOI is fully covered
    is_complete = tile_coverage.contains(original_aoi)

    # Calculate gaps
    if not is_complete:
        gaps = original_aoi.difference(tile_coverage)
        gap_area = gaps.area
        aoi_area = original_aoi.area
        gap_percentage = (gap_area / aoi_area * 100) if aoi_area > 0 else 0

        logger.warning(
            f"Coverage gaps detected: {gap_percentage:.1f}% of AOI uncovered "
            f"({gap_area:.2f} sq degrees)"
        )

        if require_full_coverage:
            raise ValueError(
                f"AOI has uncovered areas ({gap_percentage:.1f}%). "
                "Enable require_full_coverage=False to proceed anyway."
            )
    else:
        logger.info("✓ Full coverage validated - AOI completely covered")

    return {
        "is_complete": is_complete,
        "tile_count": len(tiles),
        "coverage_percentage": 100.0 if is_complete else (
            ((original_aoi.area - original_aoi.difference(tile_coverage).area) / original_aoi.area * 100)
            if original_aoi.area > 0
            else 0
        ),
    }
