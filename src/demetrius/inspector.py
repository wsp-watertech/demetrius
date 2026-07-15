"""AOI inspection and analysis without downloading tiles."""

import logging
from typing import Sequence

from .models import AOI, Tile

logger = logging.getLogger(__name__)


class InspectionReport:
    """Report on discovered tiles and datasets for an AOI."""

    def __init__(self, tiles: Sequence[Tile], aoi: AOI):
        """Create inspection report.

        Parameters
        ----------
        tiles : Sequence[Tile]
            Discovered tiles.
        aoi : AOI
            Area of interest.

        Returns
        -------
        None
            Initializes the inspection report instance.
        """
        self.tiles = tiles
        self.aoi = aoi

    def summary(self) -> str:
        """Generate human-readable summary.

        Returns
        -------
        str
            Formatted summary text.
        """
        lines = [
            "=" * 70,
            "demetrius Inspection Report",
            "=" * 70,
        ]

        # AOI info
        bounds = self.aoi.bounds()
        lines.extend(
            [
                f"\nArea of Interest:",
                f"  Bounds: ({bounds.min_x:.4f}, {bounds.min_y:.4f}) → "
                f"({bounds.max_x:.4f}, {bounds.max_y:.4f})",
                f"  Buffer: {self.aoi.buffer} m",
            ]
        )

        # Tile summary
        lines.extend(
            [
                f"\nTiles Discovered:",
                f"  Total: {len(self.tiles)} tile(s)",
            ]
        )

        if not self.tiles:
            lines.append("  (None found)")
            return "\n".join(lines)

        # Group by dataset
        datasets = {}
        for tile in self.tiles:
            if tile.dataset_id not in datasets:
                datasets[tile.dataset_id] = []
            datasets[tile.dataset_id].append(tile)

        lines.extend(
            [
                f"  Datasets: {len(datasets)}",
                f"",
                "  Dataset Priority Order (newest first):",
            ]
        )

        for i, (dataset_id, ds_tiles) in enumerate(
            sorted(datasets.items(), key=lambda x: min(t.priority for t in x[1]))
        ):
            pub_dates = [t.publication_date for t in ds_tiles]
            earliest = min(pub_dates)
            latest = max(pub_dates)

            lines.extend(
                [
                    f"    [{i}] {dataset_id}",
                    f"        Tiles: {len(ds_tiles)}",
                    f"        Publication dates: {earliest.date()} → {latest.date()}",
                ]
            )

        # CRS distribution
        lines.extend(
            [
                f"\nCoordinate Systems:",
                "  All tiles are in EPSG:4326 (WGS84)",
            ]
        )

        # Coverage estimate
        from .filtering import get_coverage_polygon

        coverage = get_coverage_polygon(self.tiles)
        covered_area = coverage.area if coverage else 0
        aoi_area = self.aoi.geometry.area

        # Calculate actual coverage of original (unbuffered) AOI
        aoi_geom = self.aoi.geometry
        if coverage:
            aoi_covered = aoi_geom.intersection(coverage)
            aoi_coverage_area = aoi_covered.area
            aoi_coverage_pct = (aoi_coverage_area / aoi_area * 100) if aoi_area > 0 else 0
        else:
            aoi_coverage_area = 0
            aoi_coverage_pct = 0

        if aoi_area > 0:
            # Raw tile area (with overlaps): sum of all individual tile areas
            raw_tile_area = sum(tile.bounds_wgs84.to_polygon().area for tile in self.tiles)

            # Overlap factor: how many times are tiles redundantly covering the same area
            overlap_factor = (raw_tile_area / covered_area) if covered_area > 0 else 0

            lines.extend(
                [
                    f"\nCoverage Estimate:",
                    f"  AOI area: {aoi_area:.2f} sq degrees",
                    f"  Actual AOI coverage: {aoi_coverage_area:.2f} sq degrees → {aoi_coverage_pct:.1f}% of AOI",
                    f"  Total tile extent (includes overhang): {covered_area:.2f} sq degrees",
                    f"  Dataset redundancy: {overlap_factor:.2f}x (minimal {(overlap_factor - 1) * 100:.0f}% overlap between datasets)",
                ]
            )

        lines.append("\n" + "=" * 70)
        return "\n".join(lines)
