"""Grid snapping for aligned DEM outputs."""

import logging
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pyproj

logger = logging.getLogger(__name__)


class Snapper:
    """Snap raster to grid aligned with cell boundaries."""

    # Linear unit conversion factors to meters (horizontal/projected CRS)
    UNIT_TO_METERS = {
        "metre": 1.0,
        "meter": 1.0,
        "m": 1.0,
        "foot": 0.3048,
        "foot_us": 0.3048006096,
        "us survey foot": 0.3048006096,
        "usfeet": 0.3048006096,
        "foot_international": 0.3048,
        "international foot": 0.3048,
    }

    @staticmethod
    def get_linear_units(crs: str) -> Optional[str]:
        """Get linear units of a projected CRS.

        Args:
            crs: CRS specification (e.g., "EPSG:32111")

        Returns:
            Unit name (e.g., "meter", "foot") or None if not found
        """
        try:
            crs_obj = pyproj.CRS(crs)
            
            # Get axis info for projected CRS
            if hasattr(crs_obj, 'axis_info') and crs_obj.axis_info:
                for axis in crs_obj.axis_info:
                    if axis.unit_name:
                        return axis.unit_name.lower()
            
            return None
        except Exception as e:
            logger.warning(f"Could not determine linear units for {crs}: {e}")
            return None

    @staticmethod
    def get_conversion_factor_for_snapping(crs: str) -> float:
        """Get conversion factor from meters to CRS linear units for snapping.

        When no cellsize is specified, we snap to 1m in CRS units.
        This means: convert 1m to the target CRS units.

        Args:
            crs: Target CRS specification

        Returns:
            Conversion factor (1m in target CRS units).
            Returns 1.0 if target is meters or units cannot be determined.
        """
        units = Snapper.get_linear_units(crs)
        if not units:
            logger.debug(f"Could not determine units for {crs}, defaulting to 1.0")
            return 1.0
        
        units_lower = units.lower().strip()
        
        # Direct lookup
        if units_lower in Snapper.UNIT_TO_METERS:
            factor = Snapper.UNIT_TO_METERS[units_lower]
            conversion = 1.0 / factor if factor > 0 else 1.0
            logger.debug(f"CRS {crs} uses {units}: 1m = {conversion:.6f} {units}")
            return conversion
        
        # Partial matches
        for key, factor in Snapper.UNIT_TO_METERS.items():
            if key.lower() in units_lower or units_lower in key.lower():
                conversion = 1.0 / factor if factor > 0 else 1.0
                logger.info(f"Matched unit '{units}' to '{key}': 1m = {conversion:.6f} {units}")
                return conversion
        
        logger.warning(f"Unknown linear unit for snapping: {units}. Defaulting to 1.0")
        return 1.0

    @staticmethod
    def get_raster_bounds(raster_path: Path) -> Tuple[float, float, float, float]:
        """Get bounds (minx, miny, maxx, maxy) from raster using gdalinfo."""
        try:
            result = subprocess.run(
                ["gdalinfo", str(raster_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            
            # Parse upper-left corner and pixel size
            bounds = None
            pixel_size = None
            
            for line in result.stdout.split('\n'):
                if line.startswith('Upper Left'):
                    # Extract coordinates: "Upper Left  (  x,  y)" format
                    import re
                    match = re.search(r'\(\s*([-\d.]+),\s*([-\d.]+)\)', line)
                    if match:
                        upper_left_x = float(match.group(1))
                        upper_left_y = float(match.group(2))
                        bounds = (upper_left_x, upper_left_y)
                
                if line.startswith('Lower Right'):
                    # Extract coordinates
                    import re
                    match = re.search(r'\(\s*([-\d.]+),\s*([-\d.]+)\)', line)
                    if match:
                        lower_right_x = float(match.group(1))
                        lower_right_y = float(match.group(2))
                        if bounds:
                            bounds = (bounds[0], lower_right_y, lower_right_x, bounds[1])
                
                if line.startswith('Pixel Size'):
                    # Extract pixel size: "Pixel Size = (x, y)"
                    import re
                    match = re.search(r'\(\s*([-\d.]+),\s*([-\d.]+)\)', line)
                    if match:
                        pixel_size = (float(match.group(1)), float(match.group(2)))
            
            if bounds and len(bounds) == 4:
                minx, miny, maxx, maxy = bounds
                return (minx, miny, maxx, maxy)
            
            raise ValueError(f"Could not parse bounds from {raster_path}")
        
        except Exception as e:
            raise RuntimeError(f"Failed to get raster bounds: {e}")

    @staticmethod
    def snap_bounds(
        minx: float,
        miny: float,
        maxx: float,
        maxy: float,
        cellsize: float,
    ) -> Tuple[float, float, float, float]:
        """Snap bounds to grid aligned with cell size.
        
        Snaps lower-left corner down/left and upper-right corner up/right
        to ensure all coordinates are exact multiples of cellsize.
        
        Args:
            minx, miny, maxx, maxy: Original bounds
            cellsize: Cell size in coordinate units
            
        Returns:
            Snapped bounds (minx, miny, maxx, maxy)
        """
        # Snap lower-left corner (floor to grid)
        snapped_minx = np.floor(minx / cellsize) * cellsize
        snapped_miny = np.floor(miny / cellsize) * cellsize
        
        # Snap upper-right corner (ceil to grid)
        snapped_maxx = np.ceil(maxx / cellsize) * cellsize
        snapped_maxy = np.ceil(maxy / cellsize) * cellsize
        
        logger.debug(
            f"Snapping bounds to grid (cellsize={cellsize}): "
            f"({minx}, {miny}, {maxx}, {maxy}) -> "
            f"({snapped_minx}, {snapped_miny}, {snapped_maxx}, {snapped_maxy})"
        )
        
        return (snapped_minx, snapped_miny, snapped_maxx, snapped_maxy)

    def snap(
        self,
        input_raster: Path,
        output_raster: Path,
        cellsize: float,
    ) -> Path:
        """Snap raster to grid aligned with cell boundaries.

        Uses gdalwarp to enforce snapped extent boundaries.

        Args:
            input_raster: Input raster path
            output_raster: Output raster path
            cellsize: Cell size in coordinate units (already converted to output CRS units)

        Returns:
            Path to snapped raster

        Raises:
            RuntimeError: If snapping fails
        """
        logger.info(f"Snapping {input_raster} to grid (cellsize: {cellsize})")
        
        # Get current bounds
        minx, miny, maxx, maxy = self.get_raster_bounds(input_raster)
        
        # Calculate snapped bounds
        snapped_minx, snapped_miny, snapped_maxx, snapped_maxy = self.snap_bounds(
            minx, miny, maxx, maxy, cellsize
        )
        
        try:
            # Use gdalwarp with -te (target extent) to enforce snapped bounds
            cmd = [
                "gdalwarp",
                "-te",
                str(snapped_minx),
                str(snapped_miny),
                str(snapped_maxx),
                str(snapped_maxy),
                "-overwrite",
                str(input_raster),
                str(output_raster),
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            
            if result.returncode != 0:
                raise ValueError(f"gdalwarp snap failed: {result.stderr}")
            
            logger.debug(f"Snapped to {output_raster}")
            return output_raster
        
        except FileNotFoundError:
            raise RuntimeError(
                "gdalwarp not found. Please install GDAL command-line tools."
            )
