"""Priority-based dataset merging with strict overwrite semantics."""

import logging
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Optional, Sequence

from .models import Tile

logger = logging.getLogger(__name__)


class DatasetMerger:
    """Merge multiple datasets with priority-based overwrite."""

    def __init__(self, working_dir: Path):
        """Initialize merger.

        Parameters
        ----------
        working_dir : Path
            Directory for working files.

        Returns
        -------
        None
            Initializes the merger instance.
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

        Parameters
        ----------
        tiles : Sequence[Tile]
            All selected tiles, ideally pre-sorted by priority.
        output_path : Path
            Output raster path.
        dataset_vrts : dict[str, Path] | None, optional
            Optional pre-created VRTs for each dataset.

        Returns
        -------
        Path
            Path to the merged raster.

        Raises
        ------
        ValueError
            If there are no datasets to merge or no merged dataset is created.
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

        # If only one dataset, no merge needed - just return the VRT
        if len(sorted_datasets) == 1:
            dataset_id, priority = sorted_datasets[0]
            dataset_tiles = dataset_groups[dataset_id]

            if dataset_vrts and dataset_id in dataset_vrts:
                dataset_raster = dataset_vrts[dataset_id]
            else:
                dataset_raster = self._prepare_dataset(dataset_id, dataset_tiles)

            logger.info(f"Single dataset; no merge needed: {dataset_raster}")
            return dataset_raster

        # Collect all dataset VRTs in priority order for a single multi-input gdalwarp call
        dataset_rasters: list[Path] = []
        for dataset_id, priority in sorted_datasets:
            dataset_tiles = dataset_groups[dataset_id]
            logger.info(
                f"Preparing dataset {dataset_id} (priority {priority}) with {len(dataset_tiles)} tiles"
            )

            if dataset_vrts and dataset_id in dataset_vrts:
                dataset_raster = dataset_vrts[dataset_id]
                logger.debug(f"Using pre-created VRT for {dataset_id}")
            else:
                dataset_raster = self._prepare_dataset(dataset_id, dataset_tiles)

            dataset_rasters.append(dataset_raster)

        # Merge all datasets in one gdalwarp call with -multi for parallelism
        logger.info(
            f"Running single multi-input gdalwarp merge for {len(dataset_rasters)} datasets"
        )
        merged_raster = self._merge_all_with_gdalwarp(dataset_rasters, output_path)

        logger.info(f"Merging complete: {merged_raster}")
        return merged_raster

    def _prepare_dataset(self, dataset_id: str, tiles: Sequence[Tile]) -> Path:
        """Prepare dataset for merging (VRT of all tiles or single tile).

        Parameters
        ----------
        dataset_id : str
            Dataset identifier.
        tiles : Sequence[Tile]
            Tiles in the dataset.

        Returns
        -------
        Path
            Path to the dataset raster or VRT.

        Raises
        ------
        ValueError
            If there are no tiles or the tiles do not have local paths.
        """
        if not tiles or not tiles[0].local_path:
            raise ValueError(f"Cannot prepare dataset {dataset_id}: no tiles with local paths")

        # Single tile: return it directly
        if len(tiles) == 1:
            return Path(tiles[0].local_path)

        # Multiple tiles: create VRT
        vrt_path = Path(tempfile.gettempdir()) / f"dataset_{dataset_id}.vrt"
        tile_paths = [str(Path(t.local_path)) for t in tiles]

        cmd = ["gdalbuildvrt", "-quiet", str(vrt_path)] + tile_paths
        subprocess.run(cmd, check=True, capture_output=True, env=os.environ.copy())

        return vrt_path

    def _merge_all_with_gdalwarp(
        self,
        dataset_rasters: list[Path],
        output_raster: Path,
    ) -> Path:
        """Merge multiple rasters in a single gdalwarp call.

        Processes all input rasters in order via gdalwarp, where later rasters
        overlay earlier ones (newer datasets overwrite older). Uses -multi for
        multithreaded resampling/warping.

        This is significantly faster than pairwise merging because:
        - Single pass instead of N-1 sequential gdalwarp calls
        - No intermediate raster materialization
        - Parallelism across all inputs simultaneously

        Parameters
        ----------
        dataset_rasters : list[Path]
            Ordered list of raster/VRT paths, from oldest (lowest priority) to
            newest (highest priority). Later entries overwrite earlier ones.
        output_raster : Path
            Output path.

        Returns
        -------
        Path
            Path to the merged raster.

        Raises
        ------
        ValueError
            If the merge fails.
        RuntimeError
            If ``gdalwarp`` is not available.
        """
        logger.debug(f"Merging {len(dataset_rasters)} rasters with gdalwarp -multi")

        try:
            cmd = [
                "gdalwarp",
                "-overwrite",
                "-multi",
                "--config",
                "CHECK_DISK_FREE_SPACE",
                "FALSE",
            ] + [str(r) for r in dataset_rasters] + [str(output_raster)]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                raise ValueError(f"gdalwarp merge failed: {result.stderr}")

            logger.debug(f"Merged to {output_raster}")
            return output_raster

        except FileNotFoundError:
            raise RuntimeError("gdalwarp not found. Please install GDAL command-line tools.")
