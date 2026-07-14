"""Elevation value conversion based on CRS vertical units."""

import logging
import subprocess
from pathlib import Path
from typing import Optional

import pyproj

logger = logging.getLogger(__name__)


class ElevationConverter:
    """Convert elevation values to match CRS vertical units using GDAL."""

    # Conversion factors to meters
    UNIT_TO_METERS = {
        "metre": 1.0,
        "meter": 1.0,
        "m": 1.0,
        "foot": 0.3048,
        "foot_us": 0.3048006096,
        "us survey foot": 0.3048006096,
        "usfeet": 0.3048006096,
    }

    @staticmethod
    def get_vertical_units(crs: str) -> Optional[str]:
        """Get vertical units of a CRS.

        Args:
            crs: CRS specification (e.g., "EPSG:32111")

        Returns:
            Unit name (e.g., "meter", "foot") or None if not found
        """
        try:
            crs_obj = pyproj.CRS(crs)
            
            # Try to get vertical CRS
            if hasattr(crs_obj, 'to_dict'):
                crs_dict = crs_obj.to_dict()
                
                # Check for vertical CRS
                if isinstance(crs_dict, dict):
                    if "vertical_crs" in crs_dict:
                        vert_dict = crs_dict["vertical_crs"]
                        if "coordinate_system" in vert_dict:
                            cs = vert_dict["coordinate_system"]
                            if "axis" in cs and len(cs["axis"]) > 0:
                                unit = cs["axis"][0].get("unit")
                                if isinstance(unit, dict) and "abbreviation" in unit:
                                    return unit["abbreviation"]
            
            # Try axis attribute
            if hasattr(crs_obj, 'axis_info') and crs_obj.axis_info:
                for axis in crs_obj.axis_info:
                    if axis.unit_name:
                        return axis.unit_name.lower()
            
            return None
        except Exception as e:
            logger.warning(f"Could not determine vertical units for {crs}: {e}")
            return None

    @staticmethod
    def get_conversion_factor(crs: str) -> float:
        """Get conversion factor from meters to CRS vertical units.

        Args:
            crs: Target CRS specification

        Returns:
            Conversion factor (multiply meters by this to get target units).
            Returns 1.0 if target is meters or units cannot be determined.
        """
        units = ElevationConverter.get_vertical_units(crs)
        if not units:
            return 1.0
        
        units_lower = units.lower().strip()
        
        # Direct lookup
        if units_lower in ElevationConverter.UNIT_TO_METERS:
            factor = ElevationConverter.UNIT_TO_METERS[units_lower]
            return 1.0 / factor if factor > 0 else 1.0
        
        # Partial matches
        for key, factor in ElevationConverter.UNIT_TO_METERS.items():
            if key.lower() in units_lower or units_lower in key.lower():
                logger.info(f"Matched unit '{units}' to '{key}'")
                return 1.0 / factor if factor > 0 else 1.0
        
        logger.warning(f"Unknown vertical unit: {units}. No conversion applied.")
        return 1.0

    def convert(
        self,
        input_raster: Path,
        output_raster: Path,
        target_crs: str,
    ) -> Path:
        """Convert elevation values in raster to target CRS vertical units.

        Uses gdal_translate with a linear -scale transform (0..1 -> 0..factor)
        to multiply all pixel values by the conversion factor. NoData pixels
        are left untouched by GDAL's -scale implementation. This avoids any
        dependency on GDAL's Python bindings (e.g. gdal_calc.py), which can
        be broken by numpy ABI mismatches in some environments, and instead
        relies solely on the pure-CLI gdal_translate binary, matching the
        rest of this codebase.

        If no conversion is needed (target CRS uses meters), the file is
        copied as-is.

        Args:
            input_raster: Input raster (elevation values in meters)
            output_raster: Output raster path
            target_crs: Target CRS for unit determination

        Returns:
            Path to converted raster

        Raises:
            RuntimeError: If conversion fails or gdal_translate is unavailable
        """
        factor = self.get_conversion_factor(target_crs)
        units = self.get_vertical_units(target_crs)
        
        logger.info(f"Elevation conversion: multiply by {factor:.6f}")
        if units:
            logger.info(f"Target units: {units}")
        
        # If no conversion needed, just copy
        if abs(factor - 1.0) < 1e-10:
            logger.info("No elevation conversion needed (target units are meters)")
            import shutil
            shutil.copy2(input_raster, output_raster)
            return output_raster
        
        logger.info(f"Converting elevation from meters to {units or 'target units'}")
        
        try:
            # Multiply every pixel value by `factor` using a linear rescale
            # from [0, 1] to [0, factor]. Using a 0..1 source range (rather
            # than an assumed elevation range like 0..9000) makes this an
            # exact multiplication regardless of the actual data range,
            # including negative elevations (e.g. below sea level).
            # NoData pixels are preserved untouched by GDAL.
            cmd = [
                "gdal_translate",
                "-ot",
                "Float32",
                "-scale",
                "0",
                "1",
                "0",
                str(factor),
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "BIGTIFF=YES",
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
                raise RuntimeError(f"Elevation conversion failed: {result.stderr}")

            logger.debug(f"Converted elevation to {output_raster}")
            return output_raster
        
        except FileNotFoundError:
            raise RuntimeError(
                "gdal_translate not found. Please install GDAL command-line tools."
            )
