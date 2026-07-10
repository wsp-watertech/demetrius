"""Priority-based dataset merging with strict overwrite semantics."""

import logging
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Optional, Sequence

from .models import Tile

logger = logging.getLogger(__name__)


class DatasetMerger:
    """Merge multiple datasets with priority-based overwrite."""

    def __init__(self, working_dir: Path):
        """Initialize merger.

        Args:
            working_dir: Directory for working files
        """
        self.working_dir = Path(working_dir)

    def merge_datasets(
        self,
        tiles: Sequence[Tile],
        output_path: Path,
        dataset_vrts: Optional[dict[str, Path]] = None,
    ) -> Path:
        """Merge tiles from multiple datasets with priority.

        Process datasets from oldest (lowest priority) to newest.
        Newer datasets completely overwrite older ones (no blending).

        Args:
            tiles: All selected tiles (should be pre-sorted by priority)
            output_path: Output raster path
            dataset_vrts: Optional pre-created VRTs for each dataset

        Returns:
            Path to merged raster
        """
        # Group by dataset and sort by priority
        dataset_groups: dict[str, list[Tile]] = defaultdict(list)
        for tile in tiles:
            dataset_groups[tile.dataset_id].append(tile)

        # Get priority order (assume tiles already sorted by priority)
        dataset_priority = {}
        for tile in tiles:
            if tile.dataset_id not in dataset_priority:
                dataset_priority[tile.dataset_id] = tile.priority

        # Sort datasets by priority (oldest first, so newest overwrites)
        sorted_datasets = sorted(dataset_priority.items(), key=lambda x: x[1])

        if not sorted_datasets:
            raise ValueError("No datasets to merge")

        logger.info(f"Merging {len(sorted_datasets)} datasets in priority order")

        # Start with first (oldest) dataset
        current_raster = None

        for i, (dataset_id, priority) in enumerate(sorted_datasets):
            dataset_tiles = dataset_groups[dataset_id]
            logger.info(f"Processing dataset {dataset_id} (priority {priority}) with {len(dataset_tiles)} tiles")

            # Use provided VRT if available, otherwise prepare from tiles
            if dataset_vrts and dataset_id in dataset_vrts:
                dataset_raster = dataset_vrts[dataset_id]
                logger.debug(f"Using pre-created VRT for {dataset_id}")
            else:
                dataset_raster = self._prepare_dataset(dataset_id, dataset_tiles)

            if i == 0:
                # First dataset - just use it directly
                current_raster = dataset_raster
            else:
                # Merge with previous using gdalwarp
                merged_path = self.working_dir / f"merged_{priority}.tif"
                current_raster = self._merge_with_gdalwarp(
                    current_raster,
                    dataset_raster,
                    merged_path,
                )

        if current_raster is None:
            raise ValueError("Failed to create merged dataset")

        logger.info(f"Merging complete: {current_raster}")
        return current_raster

    def _prepare_dataset(self, dataset_id: str, tiles: Sequence[Tile]) -> Path:
        """Prepare dataset for merging (currently returns first tile path).

        In production, would create a VRT of all tiles in the dataset.

        Args:
            dataset_id: Dataset identifier
            tiles: Tiles in dataset

        Returns:
            Path to dataset raster/VRT
        """
        if not tiles or not tiles[0].local_path:
            raise ValueError(f"Cannot prepare dataset {dataset_id}: no tiles with local paths")

        # TODO: Create VRT of all tiles
        return Path(tiles[0].local_path)

    def _merge_with_gdalwarp(
        self,
        source_raster: Path,
        overlay_raster: Path,
        output_raster: Path,
    ) -> Path:
        """Merge two rasters with overlay completely overwriting source.

        Uses gdalwarp to handle reprojection and mosaicking.

        Args:
            source_raster: Existing raster
            overlay_raster: New raster to overlay (overwrites source)
            output_raster: Output path

        Returns:
            Path to merged raster

        Raises:
            ValueError: If merge fails
        """
        logger.debug(f"Merging {overlay_raster} onto {source_raster}")

        try:
            # gdalwarp syntax: gdalwarp [options] src_file... dst_file
            # When multiple sources are provided, they are all mosaicked together
            cmd = [
                "gdalwarp",
                "-overwrite",
                str(source_raster),
                str(overlay_raster),
                str(output_raster),
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                raise ValueError(f"gdalwarp merge failed: {result.stderr}")

            logger.debug(f"Merged to {output_raster}")
            return output_raster

        except FileNotFoundError:
            raise RuntimeError(
                "gdalwarp not found. Please install GDAL command-line tools."
            )
