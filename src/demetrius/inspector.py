"""AOI inspection and analysis without downloading tiles."""

import logging
from typing import Sequence

from .models import AOI, Tile

logger = logging.getLogger(__name__)


class InspectionReport:
    """Report on discovered tiles and datasets for an AOI."""

    def __init__(self, tiles: Sequence[Tile], aoi: AOI):
        """Create inspection report.

        Args:
            tiles: Discovered tiles
            aoi: Area of Interest
        """
        self.tiles = tiles
        self.aoi = aoi

    def summary(self) -> str:
        """Generate human-readable summary.

        Returns:
            Formatted summary text
        """
        lines = [
            "=" * 70,
            "demetrius Inspection Report",
            "=" * 70,
        ]

        # AOI info
        bounds = self.aoi.bounds()
        lines.extend([
            f"\nArea of Interest:",
            f"  Bounds: ({bounds.min_x:.4f}, {bounds.min_y:.4f}) → "
            f"({bounds.max_x:.4f}, {bounds.max_y:.4f})",
            f"  Buffer distance: {self.aoi.buffer_distance} m",
        ])

        # Tile summary
        lines.extend([
            f"\nTiles Discovered:",
            f"  Total: {len(self.tiles)} tile(s)",
        ])

        if not self.tiles:
            lines.append("  (None found)")
            return "\n".join(lines)

        # Group by dataset
        datasets = {}
        for tile in self.tiles:
            if tile.dataset_id not in datasets:
                datasets[tile.dataset_id] = []
            datasets[tile.dataset_id].append(tile)

        lines.extend([
            f"  Datasets: {len(datasets)}",
            f"",
            "  Dataset Priority Order (newest first):",
        ])

        for i, (dataset_id, ds_tiles) in enumerate(
            sorted(datasets.items(), key=lambda x: min(t.priority for t in x[1]))
        ):
            pub_dates = [t.publication_date for t in ds_tiles]
            earliest = min(pub_dates)
            latest = max(pub_dates)

            lines.extend([
                f"    [{i}] {dataset_id}",
                f"        Tiles: {len(ds_tiles)}",
                f"        Publication dates: {earliest.date()} → {latest.date()}",
            ])

        # CRS distribution
        lines.extend([
            f"\nCoordinate Systems:",
            "  All tiles are in EPSG:4326 (WGS84)",
        ])

        # Coverage estimate
        from .filtering import get_coverage_polygon

        coverage = get_coverage_polygon(self.tiles)
        covered_area = coverage.area if coverage else 0
        aoi_area = self.aoi.geometry.area

        if aoi_area > 0:
            coverage_pct = (covered_area / aoi_area * 100) if covered_area > 0 else 0
            lines.extend([
                f"\nCoverage Estimate:",
                f"  AOI area: {aoi_area:.2f} sq degrees",
                f"  Tile coverage: {covered_area:.2f} sq degrees",
                f"  Coverage: {coverage_pct:.1f}%",
            ])

        lines.append("\n" + "=" * 70)
        return "\n".join(lines)
