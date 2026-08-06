"""Priority-based dataset merging with strict overwrite semantics.

Merging, reprojection, and clipping are combined into a single ``gdalwarp``
pass whenever possible. Doing all three in one call avoids materializing a
full-resolution intermediate raster (the merged-but-not-yet-reprojected
raster) and avoids resampling the data twice, which is both faster and
avoids an extra disk read/write of the largest raster in the pipeline.
"""

import json
import logging
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, Sequence

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from .models import Tile

logger = logging.getLogger(__name__)

ALLOWED_RESAMPLING_METHODS = ["bilinear", "cubic", "cubicspline", "lanczos"]


class DatasetMerger:
    """Merge multiple datasets with priority-based overwrite, optionally
    combined with reprojection and clipping in a single gdalwarp pass."""

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

    def merge_reproject_clip(
        self,
        tiles: Sequence[Tile],
        output_path: Path,
        target_crs: str,
        dataset_vrts: Optional[dict[str, Path]] = None,
        resampling: str = "bilinear",
        cellsize: Optional[float] = None,
        cutline_geometry: Optional[BaseGeometry] = None,
        snap_to_grid: bool = True,
    ) -> Path:
        """Merge datasets by priority, reproject, optionally clip and snap to grid in one pass.

        Datasets are processed from oldest (lowest priority) to newest so
        that newer datasets completely overwrite older ones in overlap areas
        (no blending) -- same semantics as a merge-then-reproject-then-clip-then-snap
        pipeline, but all done in a single gdalwarp invocation to avoid
        writing/reading intermediate rasters.

        Parameters
        ----------
        tiles : Sequence[Tile]
            All selected tiles, ideally pre-sorted by priority.
        output_path : Path
            Output raster path.
        target_crs : str
            Target CRS to reproject to, such as ``"EPSG:32618"``.
        dataset_vrts : dict[str, Path] | None, optional
            Optional pre-created VRTs for each dataset.
        resampling : str, default="bilinear"
            Resampling method, such as ``bilinear`` or ``cubic``.
        cellsize : float | None, optional
            Output cell size in target CRS units.
        cutline_geometry : BaseGeometry | None, optional
            Optional clip geometry in WGS84 (EPSG:4326). If provided, the
            output is cropped to this geometry as part of the same gdalwarp
            call.
        snap_to_grid : bool, default=True
            If True, use gdalwarp's -tap (target aligned pixels) flag to align
            output pixels to the grid, eliminating the need for a separate
            snapping step. If False, output pixels may not align to the grid.

        Returns
        -------
        Path
            Path to the merged, reprojected, clipped, and (optionally) snapped raster.

        Raises
        ------
        ValueError
            If there are no datasets to merge, or the resampling method or
            cellsize is invalid, or the gdalwarp call fails.
        RuntimeError
            If ``gdalwarp`` is not available.
        """
        if resampling not in ALLOWED_RESAMPLING_METHODS:
            raise ValueError(
                f"Resampling method '{resampling}' not allowed. "
                f"Use one of: {', '.join(ALLOWED_RESAMPLING_METHODS)}"
            )
        if cellsize is not None and cellsize <= 0:
            raise ValueError(f"Cellsize must be positive, got {cellsize}")

        dataset_rasters = self._get_priority_ordered_rasters(tiles, dataset_vrts)

        log_msg = (
            f"Merging {len(dataset_rasters)} dataset(s) and reprojecting to "
            f"{target_crs} (resampling: {resampling})"
        )
        if cutline_geometry is not None:
            log_msg += ", clipping to AOI"
        if snap_to_grid:
            log_msg += ", snapping to grid"
        log_msg += " in a single gdalwarp pass"
        logger.info(log_msg)

        cutline_path: Optional[Path] = None
        try:
            if cutline_geometry is not None:
                cutline_path = self._write_cutline_geojson(cutline_geometry)

            result_path = self._run_combined_gdalwarp(
                dataset_rasters,
                output_path,
                target_crs=target_crs,
                resampling=resampling,
                cellsize=cellsize,
                cutline_path=cutline_path,
                snap_to_grid=snap_to_grid,
            )
        finally:
            if cutline_path is not None:
                cutline_path.unlink(missing_ok=True)

        logger.info(f"Merge/reproject/clip complete: {result_path}")
        return result_path

    def _get_priority_ordered_rasters(
        self,
        tiles: Sequence[Tile],
        dataset_vrts: Optional[dict[str, Path]] = None,
    ) -> list[Path]:
        """Resolve dataset rasters/VRTs in priority order (oldest to newest).

        When tiles from a dataset use different projections, creates separate VRTs
        for each projection to avoid on-the-fly reprojection during gdalwarp.

        Parameters
        ----------
        tiles : Sequence[Tile]
            All selected tiles, ideally pre-sorted by priority.
        dataset_vrts : dict[str, Path] | None, optional
            Optional pre-created VRTs for each dataset. Can be either:
            - dict[dataset_id, Path] for single VRT per dataset (backward compat)
            - dict[(dataset_id, crs), Path] for VRTs split by CRS

        Returns
        -------
        list[Path]
            Dataset rasters/VRTs ordered from lowest to highest priority.

        Raises
        ------
        ValueError
            If there are no datasets to merge.
        """
        from .crs import get_crs_from_raster

        # Group tiles by (dataset_id, crs) to handle multi-CRS datasets
        dataset_crs_groups: dict[tuple[str, str], list[Tile]] = defaultdict(list)
        for tile in tiles:
            # Get the actual CRS from the tile file
            tile_crs = get_crs_from_raster(str(tile.local_path)) if tile.local_path else None
            if tile_crs is None:
                logger.warning(
                    f"Could not detect CRS from {tile.id if hasattr(tile, 'id') else 'tile'}"
                )
                tile_crs = "UNKNOWN"
            dataset_crs_groups[(tile.dataset_id, tile_crs)].append(tile)

        # Get priority order (assume tiles already sorted by priority)
        dataset_priority = {}
        for tile in tiles:
            if tile.dataset_id not in dataset_priority:
                dataset_priority[tile.dataset_id] = tile.priority

        if not dataset_priority:
            raise ValueError("No datasets to merge")

        dataset_rasters: list[Path] = []

        # Process datasets in priority order, but yield separate VRTs for each CRS
        for dataset_id in sorted(dataset_priority.keys(), key=lambda x: dataset_priority[x]):
            priority = dataset_priority[dataset_id]

            # Find all CRSs for this dataset
            dataset_crss = sorted(
                [crs for (ds_id, crs) in dataset_crs_groups.keys() if ds_id == dataset_id]
            )

            for crs in dataset_crss:
                dataset_crs_tiles = dataset_crs_groups[(dataset_id, crs)]

                logger.info(
                    f"Preparing dataset {dataset_id} (priority {priority}, CRS {crs}) "
                    f"with {len(dataset_crs_tiles)} tiles"
                )

                # Check for pre-created VRT (try both key formats for compatibility)
                vrt_path = None
                if dataset_vrts:
                    # Try CRS-specific key first
                    if (dataset_id, crs) in dataset_vrts:
                        vrt_path = dataset_vrts[(dataset_id, crs)]
                        logger.debug(
                            f"Using pre-created CRS-specific VRT for {dataset_id} CRS {crs}"
                        )
                    # Fall back to dataset-only key for backward compatibility
                    elif dataset_id in dataset_vrts:
                        vrt_path = dataset_vrts[dataset_id]
                        logger.debug(f"Using pre-created VRT for {dataset_id}")

                if vrt_path is None:
                    vrt_path = self._prepare_dataset(dataset_id, dataset_crs_tiles, crs=crs)

                dataset_rasters.append(vrt_path)

        return dataset_rasters

    def _prepare_dataset(
        self, dataset_id: str, tiles: Sequence[Tile], crs: Optional[str] = None
    ) -> Path:
        """Prepare dataset for merging (VRT of all tiles or single tile).

        Parameters
        ----------
        dataset_id : str
            Dataset identifier.
        tiles : Sequence[Tile]
            Tiles in the dataset (all within same CRS).
        crs : str | None, optional
            CRS identifier (e.g., "EPSG:26917"). If provided, included in VRT
            filename to distinguish VRTs for the same dataset in different CRS.

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
        # Include CRS in filename if provided to distinguish multi-CRS datasets
        crs_suffix = f"_{crs.replace(':', '_')}" if crs and crs != "UNKNOWN" else ""
        vrt_path = Path(tempfile.gettempdir()) / f"dataset_{dataset_id}{crs_suffix}.vrt"
        tile_paths = [str(Path(t.local_path)) for t in tiles]

        cmd = ["gdalbuildvrt", "-quiet", str(vrt_path)] + tile_paths
        subprocess.run(cmd, check=True, capture_output=True, env=os.environ.copy())

        return vrt_path

    def _write_cutline_geojson(self, geometry: BaseGeometry) -> Path:
        """Write a shapely geometry (WGS84) to a temporary GeoJSON file for use
        as a gdalwarp cutline.

        Parameters
        ----------
        geometry : BaseGeometry
            Shapely geometry in WGS84 (EPSG:4326).

        Returns
        -------
        Path
            Path to the temporary GeoJSON file. Caller is responsible for
            deleting it.
        """
        geojson: dict[str, Any] = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": mapping(geometry),
                }
            ],
        }

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".geojson",
            delete=False,
            dir=self.working_dir,
        ) as f:
            json.dump(geojson, f)
            return Path(f.name)

    def _run_combined_gdalwarp(
        self,
        dataset_rasters: list[Path],
        output_raster: Path,
        target_crs: str,
        resampling: str,
        cellsize: Optional[float],
        cutline_path: Optional[Path],
        snap_to_grid: bool = True,
    ) -> Path:
        """Run a single gdalwarp call that merges (priority overwrite),
        reprojects, optionally clips and snaps to grid.

        Processes all input rasters in order, where later rasters overlay
        earlier ones (newer datasets overwrite older). Uses ``-multi`` for
        multithreaded resampling/warping.

        Parameters
        ----------
        dataset_rasters : list[Path]
            Ordered list of raster/VRT paths, from oldest (lowest priority) to
            newest (highest priority). Later entries overwrite earlier ones.
        output_raster : Path
            Output path.
        target_crs : str
            Target CRS to reproject to.
        resampling : str
            Resampling method.
        cellsize : float | None
            Output cell size in target CRS units, if specified.
        cutline_path : Path | None
            Path to a GeoJSON cutline file, if clipping is requested.
        snap_to_grid : bool, default=True
            If True, use -tap (target aligned pixels) flag to align output
            pixels to the grid.

        Returns
        -------
        Path
            Path to the merged/reprojected/clipped/snapped raster.

        Raises
        ------
        ValueError
            If the gdalwarp call fails.
        RuntimeError
            If ``gdalwarp`` is not available.
        """
        try:
            cmd = [
                "gdalwarp",
                "-overwrite",
                "-t_srs",
                target_crs,
                "-r",
                resampling,
                "-multi",
                "-wo",
                "NUM_THREADS=ALL_CPUS",
                "-wm",
                "2000",  # 2GB working memory for efficient block processing
                "-co",
                "TILED=YES",
                "-co",
                "BIGTIFF=YES",  # Support files > 4GB
                # No compression here: this is an ephemeral intermediate raster,
                # immediately re-read and discarded by the next pipeline stage.
                # Compressing it costs CPU time on both write and the subsequent
                # read with no lasting benefit; only the final COG output is
                # compressed. Requires adequate scratch disk space.
            ]

            if snap_to_grid:
                cmd.append("-tap")  # Target aligned pixels

            if cellsize is not None:
                # Round to 9 decimal places to avoid floating-point precision artifacts
                cellsize_str = f"{cellsize:.9f}".rstrip("0").rstrip(".")
                cmd.extend(["-tr", cellsize_str, cellsize_str])

            if cutline_path is not None:
                cmd.extend(["-cutline", str(cutline_path), "-crop_to_cutline"])

            cmd.extend([str(r) for r in dataset_rasters])
            cmd.append(str(output_raster))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                raise ValueError(f"gdalwarp merge/reproject/clip/snap failed: {result.stderr}")

            return output_raster

        except FileNotFoundError:
            raise RuntimeError("gdalwarp not found. Please install GDAL command-line tools.")
