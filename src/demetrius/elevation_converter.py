"""Elevation value conversion based on CRS linear units."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import pyproj

logger = logging.getLogger(__name__)


class ElevationConverter:
    """Convert elevation values to match CRS linear units using GDAL."""

    # Conversion factors to meters (linear/horizontal CRS units)
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

        For projected CRS, elevation is measured in the same units as the
        horizontal coordinates (e.g., meters for UTM, feet for State Plane).
        This method returns those horizontal CRS units.

        Parameters
        ----------
        crs : str
            CRS specification, such as ``"EPSG:32111"``.

        Returns
        -------
        str | None
            Unit name, such as ``"meter"`` or ``"foot"``, or ``None`` if not
            found.
        """
        try:
            crs_obj = pyproj.CRS(crs)

            # Get axis info for projected CRS linear units
            if hasattr(crs_obj, "axis_info") and crs_obj.axis_info:
                for axis in crs_obj.axis_info:
                    if axis.unit_name:
                        return axis.unit_name.lower()

            return None
        except Exception as e:
            logger.warning(f"Could not determine linear units for {crs}: {e}")
            return None

    @staticmethod
    def get_conversion_factor(crs: str) -> float:
        """Get conversion factor from meters to CRS linear units.

        Parameters
        ----------
        crs : str
            Target CRS specification.

        Returns
        -------
        float
            Conversion factor used to multiply meters into target units.
            Returns ``1.0`` if the target already uses meters or the units
            cannot be determined.
        """
        units = ElevationConverter.get_linear_units(crs)
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

        logger.warning(f"Unknown linear unit: {units}. No conversion applied.")
        return 1.0

    def convert_and_generate_cog(
        self,
        input_raster: Path,
        output_cog: Path,
        target_crs: str,
        compression: str = "deflate",
        blocksize: int = 512,
        generate_overviews: bool = True,
    ) -> Path:
        """Convert elevation values and generate a Cloud-Optimized GeoTIFF in one pass.

        Combines elevation unit conversion (meters → target CRS units) with COG
        generation to avoid materializing an intermediate raster. Uses
        gdal_translate with -scale to multiply pixel values by the conversion
        factor, while simultaneously creating a COG with tiling, compression, and
        (optionally) overviews.

        Parameters
        ----------
        input_raster : Path
            Input raster with elevation values in meters.
        output_cog : Path
            Output COG path.
        target_crs : str
            Target CRS used for unit determination.
        compression : str, default="deflate"
            Compression method, such as ``deflate`` or ``lzw``.
        blocksize : int, default=512
            Internal tile size in pixels.
        generate_overviews : bool, default=True
            Whether to build overview pyramids in the COG. Overviews speed up
            zoomed-out rendering in GIS/COG viewers but require additional
            downsampled reads of the full raster, adding meaningful I/O time
            for large DEMs. Set to False to skip them if the output is
            primarily consumed by tools that read at full resolution (e.g.
            hydrologic models) rather than interactively viewed.

        Returns
        -------
        Path
            Path to the generated COG with converted elevation values.

        Raises
        ------
        RuntimeError
            If conversion/COG generation fails or ``gdal_translate`` is unavailable.
        """
        factor = self.get_conversion_factor(target_crs)
        units = self.get_linear_units(target_crs)

        logger.info(f"Converting elevation and generating COG: {output_cog}")
        logger.info(f"Elevation conversion factor: {factor:.6f}")
        if units:
            logger.info(f"Target units: {units}")

        try:
            cmd = [
                "gdal_translate",
                "-of",
                "COG",
                "-ot",
                "Float32",
                "-co",
                f"COMPRESS={compression.upper()}",
                "-co",
                f"BLOCKSIZE={blocksize}",
                "-co",
                "NUM_THREADS=ALL_CPUS",  # Parallelize compression encoding
                "-co",
                "BIGTIFF=YES",  # Support files > 4GB
            ]

            # Floating-point predictor improves both compression ratio and
            # encode/decode speed for continuous data like elevation. Only
            # applies to predictor-aware compressors.
            if compression.lower() in ("deflate", "lzw", "zstd"):
                cmd.extend(["-co", "PREDICTOR=3"])

            if not generate_overviews:
                logger.info("Skipping overview pyramid generation (generate_overviews=False)")
                cmd.extend(["-co", "OVERVIEWS=NONE"])

            # Apply elevation conversion if needed
            if abs(factor - 1.0) >= 1e-10:
                logger.info(f"Converting elevation from meters to {units or 'target units'}")
                # Scale 0..1 to 0..factor (exact multiplication regardless of data range)
                cmd.extend(["-scale", "0", "1", "0", str(factor)])
            else:
                logger.info("No elevation conversion needed (target units are meters)")

            cmd.extend([str(input_raster), str(output_cog)])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"Elevation conversion and COG generation failed: {result.stderr}"
                )

            logger.info(f"✓ Generated COG with converted elevation: {output_cog}")
            logger.debug(f"File size: {output_cog.stat().st_size / 1024 / 1024:.1f} MB")

            return output_cog

        except FileNotFoundError:
            raise RuntimeError("gdal_translate not found. Please install GDAL command-line tools.")
