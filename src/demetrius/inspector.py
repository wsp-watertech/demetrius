"""AOI inspection and analysis without downloading tiles."""

import logging
from typing import Sequence

from .models import AOI, Tile

logger = logging.getLogger(__name__)


def format_bytes(num_bytes: float) -> str:
    """Format bytes as human-readable string.

    Parameters
    ----------
    num_bytes : float
        Number of bytes.

    Returns
    -------
    str
        Formatted string (e.g., "42.5 MB", "1.2 GB").
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


class InspectionReport:
    """Report on discovered tiles and datasets for an AOI."""

    # Estimated compressed GeoTIFF size per tile (1m DEM, 1x1 degree)
    # Typical USGS 3DEP tiles with COG compression
    BYTES_PER_TILE = 85 * 1024 * 1024  # ~85 MB

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

    def estimate_download_size(self) -> float:
        """Estimate total download size in bytes.

        Uses a conservative estimate of 85 MB per 1m DEM tile (1x1 degree).

        Returns
        -------
        float
            Estimated download size in bytes.
        """
        return len(self.tiles) * self.BYTES_PER_TILE

    def estimate_dem_size(self, cellsize: float = 0.00001) -> float:
        """Estimate output DEM size in bytes.

        Assumes Float32 pixel format (4 bytes per pixel) for Cloud-Optimized GeoTIFF.
        Cellsize is in the units of the bounds (WGS84 degrees).

        Parameters
        ----------
        cellsize : float, default=0.00001
            Cell size in degrees. Default 0.00001 ≈ 1 meter at equator.

        Returns
        -------
        float
            Estimated output DEM size in bytes.
        """
        bounds = self.aoi.bounds()
        width_deg = bounds.max_x - bounds.min_x
        height_deg = bounds.max_y - bounds.min_y

        # Number of pixels
        width_pixels = int(width_deg / cellsize)
        height_pixels = int(height_deg / cellsize)

        # 4 bytes per pixel for Float32 COG
        return width_pixels * height_pixels * 4

    def estimate_dem_size_clipped(self, cellsize: float = 0.00001) -> float:
        """Estimate output DEM size after clipping to AOI polygon.

        Factors in the actual polygon coverage vs. the bounding box.
        Assumes uniform pixel distribution across the polygon area.

        Parameters
        ----------
        cellsize : float, default=0.00001
            Cell size in degrees. Default 0.00001 ≈ 1 meter at equator.

        Returns
        -------
        float
            Estimated output DEM size in bytes after clipping to polygon.
        """
        # Get bounding box estimate first
        bbox_estimate = self.estimate_dem_size(cellsize)

        # Calculate actual polygon area vs. bounding box area
        bounds = self.aoi.bounds()
        bbox_area = (bounds.max_x - bounds.min_x) * (bounds.max_y - bounds.min_y)

        # Polygon area (in same units as bounds)
        polygon_area = self.aoi.geometry.area

        # Coverage fraction
        coverage_fraction = polygon_area / bbox_area if bbox_area > 0 else 1.0

        # Clipped estimate
        return bbox_estimate * coverage_fraction

    def estimate_dem_size_clipped_compressed(self, cellsize: float = 0.00001, compression_ratio: float = 0.35) -> float:
        """Estimate output DEM size after clipping and compression.

        Factors in deflate/LZW compression typical of Cloud-Optimized GeoTIFF.

        Parameters
        ----------
        cellsize : float, default=0.00001
            Cell size in degrees. Default 0.00001 ≈ 1 meter at equator.
        compression_ratio : float, default=0.35
            Fraction of uncompressed size after compression.
            Default 0.35 = ~35% of original (3:1 compression ratio).
            Typical range for elevation data: 0.25-0.45 (2-4x compression).

        Returns
        -------
        float
            Estimated output DEM size in bytes after clipping and compression.
        """
        clipped_estimate = self.estimate_dem_size_clipped(cellsize)
        return clipped_estimate * compression_ratio

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
                "\nArea of Interest:",
                f"  Bounds: ({bounds.min_x:.4f}, {bounds.min_y:.4f}) → "
                f"({bounds.max_x:.4f}, {bounds.max_y:.4f})",
                f"  Buffer: {self.aoi.buffer} m",
            ]
        )

        # Tile summary
        lines.extend(
            [
                "\nTiles Discovered:",
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
                "",
                "  Dataset Priority Order (oldest first, newer overlays older):",
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
                "\nCoordinate Systems:",
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
                    "\nCoverage Estimate:",
                    f"  AOI area: {aoi_area:.2f} sq degrees",
                    f"  Actual AOI coverage: {aoi_coverage_area:.2f} sq degrees → {aoi_coverage_pct:.1f}% of AOI",
                    f"  Total tile extent (includes overhang): {covered_area:.2f} sq degrees",
                    f"  Dataset redundancy: {overlap_factor:.2f}x ({(overlap_factor - 1) * 100:.0f}% overlap between datasets)",
                ]
            )

        # Size estimates
        download_size = self.estimate_download_size()
        dem_size = self.estimate_dem_size()
        dem_size_clipped = self.estimate_dem_size_clipped()
        dem_size_clipped_compressed = self.estimate_dem_size_clipped_compressed()

        # Calculate clipping factor for display
        clip_factor = dem_size_clipped / dem_size if dem_size > 0 else 1.0

        lines.extend(
            [
                "\nSize Estimates:",
                f"  Download size: {format_bytes(download_size)}",
                "  Output DEM size (1m, Float32):",
                f"    - Bounding box (filled): {format_bytes(dem_size)}",
                f"    - Clipped to polygon (uncompressed): {format_bytes(dem_size_clipped)} ({clip_factor*100:.1f}% of bbox)",
                f"    - Clipped to polygon (compressed COG): {format_bytes(dem_size_clipped_compressed)}",
                "  (Estimates assume 1m resolution; COG uses ~3:1 compression)",
            ]
        )

        lines.append("\n" + "=" * 70)
        return "\n".join(lines)
