"""Reprojection of rasters using GDAL."""

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Reprojector:
    """Reproject rasters to target CRS using GDAL."""

    def reproject(
        self,
        input_raster: Path,
        output_raster: Path,
        target_crs: str,
        resampling: str = "bilinear",
        cellsize: float | None = None,
    ) -> Path:
        """Reproject raster to target CRS and optionally set output cellsize.

        Uses gdalwarp for efficient reprojection with support for
        multi-threaded execution.

        Args:
            input_raster: Input raster path
            output_raster: Output raster path
            target_crs: Target CRS (e.g., "EPSG:32618")
            resampling: Resampling method (bilinear, cubic, etc.)
            cellsize: Output cellsize in target CRS units (optional)

        Returns:
            Path to reprojected raster

        Raises:
            ValueError: If resampling method is invalid or cellsize is invalid
            RuntimeError: If gdalwarp fails or is not available
        """
        # Validate resampling method
        allowed_methods = ["bilinear", "cubic", "cubicspline", "lanczos"]
        if resampling not in allowed_methods:
            raise ValueError(
                f"Resampling method '{resampling}' not allowed. "
                f"Use one of: {', '.join(allowed_methods)}"
            )

        # Validate cellsize
        if cellsize is not None and cellsize <= 0:
            raise ValueError(f"Cellsize must be positive, got {cellsize}")

        log_msg = f"Reprojecting {input_raster} to {target_crs} (resampling: {resampling})"
        if cellsize is not None:
            log_msg += f", cellsize: {cellsize}"
        logger.info(log_msg)

        try:
            cmd = [
                "gdalwarp",
                "-t_srs",
                target_crs,
                "-r",
                resampling,
                "-multi",
                "-wo",
                "NUM_THREADS=ALL_CPUS",
                "-overwrite",
            ]

            # Add cellsize (target resolution) if specified
            # Round to 9 decimal places to avoid floating-point precision artifacts
            if cellsize is not None:
                cellsize_str = f"{cellsize:.9f}".rstrip('0').rstrip('.')
                cmd.extend(["-tr", cellsize_str, cellsize_str])

            cmd.extend([str(input_raster), str(output_raster)])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                raise ValueError(f"gdalwarp failed: {result.stderr}")

            logger.debug(f"Reprojected to {output_raster}")
            return output_raster

        except FileNotFoundError:
            raise RuntimeError(
                "gdalwarp not found. Please install GDAL command-line tools."
            )
