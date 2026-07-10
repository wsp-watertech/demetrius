"""Coverage validation and gap detection."""

import logging
from typing import Optional, Sequence

from shapely.geometry import Polygon

from .filtering import get_coverage_polygon
from .models import AOI, Tile
from .project_boundaries import ProjectBoundaries

logger = logging.getLogger(__name__)


def validate_coverage(
    tiles: Sequence[Tile],
    aoi: AOI,
    project_bounds: Optional[ProjectBoundaries] = None,
    require_full_coverage: bool = False,
) -> dict[str, bool | float]:
    """Validate tile coverage of original AOI.

    If project_bounds is provided, checks coverage against actual project
    boundaries. Otherwise, uses tile bounding boxes.

    Checks that the original (unbuffered) AOI has adequate coverage.
    Buffer gaps are allowed, but AOI gaps warrant warnings.

    Args:
        tiles: Selected tiles
        aoi: Area of Interest
        project_bounds: Optional project boundaries for coverage-aware validation
        require_full_coverage: If True, raise error on gaps; if False, just warn

    Returns:
        Coverage report with 'is_complete' and 'coverage_percentage'

    Raises:
        ValueError: If require_full_coverage=True and gaps exist
    """
    logger.info(f"Validating coverage for {len(tiles)} tiles")

    # Get original AOI polygon (not buffered)
    original_aoi = aoi.geometry

    if project_bounds is not None:
        # Use actual project coverage instead of tile bboxes
        project_coverage = project_bounds.get_coverage_for_geometry(original_aoi)
        coverage_fraction = project_bounds.coverage_fraction(original_aoi)
        is_complete = project_bounds.covers_geometry(original_aoi, min_coverage=0.95)

        if not is_complete:
            logger.warning(
                f"Coverage gaps detected: {coverage_fraction * 100:.1f}% of AOI covered by projects"
            )
            if require_full_coverage:
                raise ValueError(
                    f"AOI is only {coverage_fraction * 100:.1f}% covered by project boundaries"
                )
        else:
            logger.info(
                f"✓ Full coverage validated - AOI {coverage_fraction * 100:.1f}% covered by projects"
            )

        return {
            "is_complete": is_complete,
            "tile_count": len(tiles),
            "coverage_percentage": coverage_fraction * 100,
        }
    else:
        # Fall back to tile bbox coverage
        tile_coverage = get_coverage_polygon(tiles)

        is_complete = tile_coverage.contains(original_aoi)

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
            "coverage_percentage": (
                100.0
                if is_complete
                else (
                    (
                        (
                            original_aoi.area
                            - original_aoi.difference(tile_coverage).area
                        )
                        / original_aoi.area
                        * 100
                    )
                    if original_aoi.area > 0
                    else 0
                )
            ),
        }
