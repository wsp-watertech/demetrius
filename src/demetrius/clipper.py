"""Clipping rasters to AOI geometry."""

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)


class Clipper:
    """Clip rasters to AOI geometry using GDAL."""

    def clip(
        self,
        input_raster: Path,
        output_raster: Path,
        geometry: BaseGeometry,
    ) -> Path:
        """Clip raster to AOI geometry.

        Uses gdalwarp with -cutline option for precise clipping.

        Args:
            input_raster: Input raster path
            output_raster: Output clipped raster path
            geometry: Shapely geometry to clip to

        Returns:
            Path to clipped raster

        Raises:
            RuntimeError: If clipping fails or gdalwarp is not available
        """
        logger.info(f"Clipping {input_raster} to AOI geometry")

        # Convert geometry to GeoJSON for gdalwarp
        geojson = self._geometry_to_geojson(geometry)

        try:
            # Create temporary GeoJSON file
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".geojson",
                delete=False,
            ) as f:
                json.dump(geojson, f)
                geojson_path = f.name

            try:
                cmd = [
                    "gdalwarp",
                    "-cutline",
                    geojson_path,
                    "-crop_to_cutline",
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
                    raise RuntimeError(f"gdalwarp clipping failed: {result.stderr}")

                logger.debug(f"Clipped to {output_raster}")
                return output_raster

            finally:
                # Clean up temporary file
                Path(geojson_path).unlink(missing_ok=True)

        except FileNotFoundError:
            raise RuntimeError(
                "gdalwarp not found. Please install GDAL command-line tools."
            )

    def _geometry_to_geojson(self, geometry: BaseGeometry) -> dict[str, Any]:
        """Convert shapely geometry to GeoJSON FeatureCollection.

        Args:
            geometry: Shapely geometry object

        Returns:
            GeoJSON FeatureCollection
        """
        from shapely.geometry import mapping

        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": mapping(geometry),
                }
            ],
        }
