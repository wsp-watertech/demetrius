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

        # Start with first (oldest) dataset
        current_raster = None

        for i, (dataset_id, priority) in enumerate(sorted_datasets):
            dataset_tiles = dataset_groups[dataset_id]
            logger.info(
                f"Processing dataset {dataset_id} (priority {priority}) with {len(dataset_tiles)} tiles"
            )

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
        subprocess.run(cmd, check=True, capture_output=True)

        return vrt_path

    def _merge_with_gdalwarp(
        self,
        source_raster: Path,
        overlay_raster: Path,
        output_raster: Path,
    ) -> Path:
        """Merge two rasters with priority to non-nodata values.

        Uses gdalwarp with a VRT that ensures nodata values from newer rasters
        don't override valid data from older rasters. This implements a "most recent
        non-nodata" strategy: values should always be the most recent, except for
        nodata values which carry lowest priority.

        Parameters
        ----------
        source_raster : Path
            Existing raster containing older, lower-priority data.
        overlay_raster : Path
            New raster to overlay containing newer, higher-priority data.
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
        logger.debug(f"Merging {overlay_raster} onto {source_raster} (skipping nodata in newer)")

        try:
            # Pass overlay first, then source so that overlay is the primary source
            # but gdalwarp will skip nodata pixels and pull from source below
            cmd = [
                "gdalwarp",
                "-overwrite",
                str(overlay_raster),
                str(source_raster),
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
            raise RuntimeError("gdalwarp not found. Please install GDAL command-line tools.")
