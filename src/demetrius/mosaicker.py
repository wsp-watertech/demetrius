"""VRT-based mosaicking for efficient large-scale raster merging."""

import logging
import subprocess
from pathlib import Path
from typing import Sequence

from .models import Tile

logger = logging.getLogger(__name__)


class VRTMosaicker:
    """Create Virtual Raster (VRT) files for efficient tile mosaicking."""

    def __init__(self, working_dir: Path):
        """Initialize mosaicker.

        Args:
            working_dir: Directory for VRT files
        """
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def create_dataset_vrt(self, dataset_id: str, tiles: Sequence[Tile]) -> Path:
        """Create a VRT file for all tiles in a dataset.

        Uses gdalbuildvrt for efficient virtual mosaicking without
        loading data into memory.

        Args:
            dataset_id: Dataset identifier
            tiles: Tiles in this dataset (all must have local_path set)

        Returns:
            Path to created VRT file

        Raises:
            ValueError: If VRT creation fails
            RuntimeError: If gdalbuildvrt is not available
        """
        # Validate inputs
        if not tiles:
            raise ValueError(f"No tiles provided for dataset {dataset_id}")

        for tile in tiles:
            if not tile.local_path:
                raise ValueError(f"Tile {tile.id} has no local_path set")
            if not Path(tile.local_path).exists():
                raise ValueError(f"Tile {tile.id} file not found: {tile.local_path}")

        vrt_path = self.working_dir / f"dataset_{dataset_id}.vrt"

        # Get tile paths
        tile_paths = [str(tile.local_path) for tile in tiles]

        logger.info(f"Creating VRT for dataset {dataset_id} with {len(tiles)} tiles")

        try:
            cmd = ["gdalbuildvrt", str(vrt_path)] + tile_paths
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                raise ValueError(f"gdalbuildvrt failed: {result.stderr}")

            logger.debug(f"Created VRT: {vrt_path}")
            return vrt_path

        except FileNotFoundError:
            raise RuntimeError(
                "gdalbuildvrt not found. Please install GDAL command-line tools."
            )

    def create_merged_vrt(
        self,
        dataset_vrts: dict[str, Path],
        output_vrt: Path,
    ) -> Path:
        """Merge multiple dataset VRTs with priority.

        Creates a layered VRT that respects dataset priority (newer overwrites older).

        Args:
            dataset_vrts: Mapping of dataset_id to VRT file path
            output_vrt: Path for merged VRT output

        Returns:
            Path to merged VRT

        Raises:
            ValueError: If merge fails
        """
        if not dataset_vrts:
            raise ValueError("No dataset VRTs provided")

        logger.info(f"Merging {len(dataset_vrts)} dataset VRTs")

        # For MVP, create a simple concatenation VRT
        # More sophisticated priority handling via gdalwarp in the merge step
        vrt_paths = list(dataset_vrts.values())

        try:
            cmd = ["gdalbuildvrt", str(output_vrt)] + [str(p) for p in vrt_paths]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                raise ValueError(f"VRT merge failed: {result.stderr}")

            logger.debug(f"Created merged VRT: {output_vrt}")
            return output_vrt

        except FileNotFoundError:
            raise RuntimeError(
                "gdalbuildvrt not found. Please install GDAL command-line tools."
            )
