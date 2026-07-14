"""Cloud-Optimized GeoTIFF (COG) generation."""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class COGGenerator:
    """Generate Cloud-Optimized GeoTIFF from raster."""

    def generate(
        self,
        input_raster: Path,
        output_cog: Path,
        compression: str = "deflate",
        blocksize: int = 512,
    ) -> Path:
        """Generate Cloud-Optimized GeoTIFF.

        Uses gdal_translate to create a COG with:
        - Internal tiling
        - Overviews for efficient zooming
        - Compression for smaller file size
        - Valid NODATA handling

        Args:
            input_raster: Input raster path
            output_cog: Output COG path
            compression: Compression method (deflate, lzw, etc.)
            blocksize: Internal tile size in pixels

        Returns:
            Path to generated COG

        Raises:
            RuntimeError: If COG generation fails or gdal_translate is not available
        """
        logger.info(f"Generating COG: {output_cog}")

        try:
            cmd = [
                "gdal_translate",
                "-of",
                "COG",
                "-co",
                f"COMPRESS={compression.upper()}",
                "-co",
                f"BLOCKSIZE={blocksize}",
                str(input_raster),
                str(output_cog),
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                raise RuntimeError(f"gdal_translate failed: {result.stderr}")

            logger.info(f"✓ Generated COG: {output_cog}")
            logger.debug(f"File size: {output_cog.stat().st_size / 1024 / 1024:.1f} MB")

            return output_cog

        except FileNotFoundError:
            raise RuntimeError(
                "gdal_translate not found. Please install GDAL command-line tools."
            )
