"""Core DEM generation pipeline.

This module contains the actual query -> download -> mosaic ->
merge/reproject/clip (single gdalwarp pass) -> snap -> COG workflow as a
reusable library function, independent of the CLI. ``cli.py`` and
``batch.py`` both build on :func:`run_pipeline`.
"""

import logging
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional, Union

from .models import AOI
from .project_boundaries import ProjectBoundaries

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

PipelineMode = Literal["full", "download-only", "process-only"]
PipelineStatus = Literal["success", "failed"]


def _ensure_tmpdir_valid() -> None:
    """Validate that TMPDIR (if set) points to an existing, writable directory.

    Python's ``tempfile`` module silently falls back to the system default
    (e.g. ``/tmp``) if ``TMPDIR`` points to a directory that doesn't exist or
    isn't writable -- it never raises an error. This means a mistyped or
    not-yet-created ``TMPDIR`` (a common issue when scratch volumes are
    mounted after the fact, or under ``nohup``/non-interactive shells) fails
    silently and large jobs end up writing to a small ``/tmp`` anyway.

    This creates the directory (including parents) if it doesn't exist, and
    raises a clear error if it can't be created or isn't writable, rather
    than letting GDAL/tempfile silently fall back elsewhere.

    Raises
    ------
    RuntimeError
        If TMPDIR is set but cannot be created or is not writable.
    """
    tmpdir_env = os.environ.get("TMPDIR")
    if not tmpdir_env:
        return

    tmpdir_path = Path(tmpdir_env)
    try:
        tmpdir_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"TMPDIR is set to '{tmpdir_env}' but the directory could not be created: {e}"
        ) from e

    if not os.access(tmpdir_path, os.W_OK):
        raise RuntimeError(
            f"TMPDIR is set to '{tmpdir_env}' but it is not writable. "
            "tempfile/GDAL will silently fall back to the system default temp "
            "directory (e.g. /tmp) if this is not fixed, which can exhaust disk "
            "space on large jobs."
        )

    # Clear tempfile's cached tempdir so the (possibly newly created) TMPDIR
    # is picked up even if gettempdir() was already called earlier in this process.
    tempfile.tempdir = None


@dataclass
class PipelineResult:
    """Outcome of running the pipeline for a single AOI.

    Attributes
    ----------
    name : str
        Name used to label this run (e.g. AOI shortname).
    status : {"success", "failed"}
        Whether the pipeline completed successfully.
    output_path : Path | None
        Path to the generated COG, if produced.
    manifest_path : Path | None
        Path to the saved manifest, if produced.
    tile_count : int
        Number of tiles used.
    dataset_count : int
        Number of unique datasets used.
    error : str | None
        Error message, if the run failed.
    """

    name: str
    status: PipelineStatus
    output_path: Optional[Path] = None
    manifest_path: Optional[Path] = None
    tile_count: int = 0
    dataset_count: int = 0
    error: Optional[str] = None


def run_pipeline(
    aoi: Union[AOI, str, Path],
    output: Union[str, Path],
    *,
    name: str = "dem",
    output_crs: Optional[str] = None,
    ffrd: bool = False,
    buffer: int = 0,
    cellsize: Optional[float] = None,
    no_snap: bool = False,
    no_clip: bool = False,
    no_overviews: bool = False,
    project_bounds: Optional[Union[ProjectBoundaries, str, Path]] = None,
    require_full_coverage: bool = False,
    data_dir: Optional[Union[str, Path]] = None,
    mode: PipelineMode = "full",
    progress_cb: Optional[ProgressCallback] = None,
) -> PipelineResult:
    """Run the full demetrius DEM pipeline for a single AOI.

    Parameters
    ----------
    aoi : AOI | str | Path
        An already-constructed :class:`~demetrius.models.AOI`, or a path to a
        geometry file to load (buffer is applied when loading from a path).
    output : str | Path
        Output COG path.
    name : str, default="dem"
        Label used for logging/progress messages and in the returned result.
    output_crs : str | None
        Target CRS (e.g. ``"EPSG:32618"``). Ignored if ``ffrd=True``.
    ffrd : bool, default=False
        Use the bundled FFRD projection, forcing snapping and a default
        cellsize of 4 unless overridden.
    buffer : int, default=0
        Buffer distance in output CRS units for tile discovery and clipping.
        Only used when ``aoi`` is a path; ignored if ``aoi`` is already an
        :class:`AOI` instance.
    cellsize : float | None
        Output cellsize in target CRS units.
    no_snap : bool, default=False
        Disable grid snapping.
    no_clip : bool, default=False
        Disable clipping to AOI. If True, output is full merged/reprojected extent
        rather than clipped to the buffered AOI.
    no_overviews : bool, default=False
        Skip building overview pyramids in the output COG. Overviews speed up
        zoomed-out rendering in GIS/COG viewers but require additional
        downsampled reads of the full raster during generation. Useful to
        disable for outputs primarily consumed by tools reading at full
        resolution (e.g. hydrologic models).
    project_bounds : ProjectBoundaries | str | Path | None
        Project boundaries instance, or a path to load one from.
    require_full_coverage : bool, default=False
        Require full coverage of the original AOI by project boundaries.
    data_dir : str | Path | None
        Directory for downloaded tiles. Defaults to ``DEMETRIUS_DATA_DIR`` env var or ``~/.demetrius``.
    mode : {"full", "download-only", "process-only"}
        Which pipeline stages to run.
    progress_cb : Callable[[str], None] | None
        Optional callback invoked with human-readable progress messages.

    Returns
    -------
    PipelineResult
        Structured result describing success/failure and output locations.
        Expected failures (bad input, coverage gaps, no tiles found, etc.)
        are captured here rather than raised.
    """

    def _report(message: str) -> None:
        logger.info(f"[{name}] {message}")
        if progress_cb is not None:
            progress_cb(message)

    try:
        _ensure_tmpdir_valid()

        from .coverage import validate_coverage
        from .crs import get_target_utm_for_tiles
        from .downloader import TileDownloader
        from .elevation_converter import ElevationConverter
        from .filtering import filter_tiles_by_aoi
        from .manifest import Manifest
        from .merger import DatasetMerger
        from .mosaicker import VRTMosaicker
        from .priority import prioritize_datasets
        from .projections import get_projection_file
        from .snapper import Snapper
        from .tnm import TNMTileSource

        output_path = Path(output)

        # Resolve AOI
        if isinstance(aoi, AOI):
            aoi_obj = aoi
        else:
            aoi_obj = AOI.from_file(str(aoi), buffer=buffer)

        # Resolve project bounds
        proj_bounds: Optional[ProjectBoundaries]
        if isinstance(project_bounds, ProjectBoundaries) or project_bounds is None:
            proj_bounds = project_bounds
        else:
            proj_bounds = ProjectBoundaries.from_file(str(project_bounds))

        # Handle --ffrd behavior
        if ffrd:
            if output_crs:
                _report("⚠ ffrd=True overrides output_crs")
            ffrd_path = get_projection_file("ffrd.prj")
            if not ffrd_path:
                raise RuntimeError("FFRD projection file not found")

            ffrd_wkt = ffrd_path.read_text().strip()
            if not ffrd_wkt:
                raise ValueError("FFRD projection file is empty")

            output_crs = ffrd_wkt
            _report("Using FFRD custom projection")

            if no_snap:
                _report("⚠ ffrd=True requires snapping (no_snap ignored)")
            no_snap = False

            if cellsize is None:
                cellsize = 4.0
                _report("Using FFRD default cellsize: 4")

        # Determine target CRS early (needed for buffering and cellsize conversion)
        if not output_crs:
            _report("Auto-detecting target CRS...")
            source = TNMTileSource()
            initial_bbox = aoi_obj.bounds()
            initial_tiles = source.search(initial_bbox)
            if initial_tiles:
                output_crs = get_target_utm_for_tiles(initial_tiles)
                _report(f"Auto-selected target CRS: {output_crs}")
            else:
                raise ValueError("No tiles found for CRS auto-detection")
        else:
            _report(f"Using target CRS: {output_crs}")

        # Compute default cellsize if not specified (1m converted to output_crs units)
        effective_cellsize = cellsize
        if cellsize is None:
            snapper = Snapper()
            effective_cellsize = snapper.get_conversion_factor_for_snapping(output_crs)
            _report(
                f"Using default cellsize ({'snapping disabled' if no_snap else 'snapping enabled'}): "
                f"1m in {output_crs} units = {effective_cellsize:.9f}"
            )

        # Step 1: Query TNM
        if mode != "process-only":
            _report("Querying TNM for tiles...")
            source = TNMTileSource()

            if buffer > 0:
                try:
                    buffered_geom_in_output_crs = aoi_obj.buffered_geometry_in_crs(output_crs)
                    bounds = buffered_geom_in_output_crs.bounds
                    if any(b == float("inf") or b == float("-inf") for b in bounds):
                        buffered_bbox = aoi_obj.buffered_bounds(output_crs=output_crs)
                    else:
                        import geopandas as gpd

                        gdf = gpd.GeoDataFrame(
                            [{"geometry": buffered_geom_in_output_crs}], crs=output_crs
                        )
                        gdf_wgs84 = gdf.to_crs("EPSG:4326")
                        buffered_geom_wgs84 = gdf_wgs84.iloc[0].geometry
                        from .models import BoundingBox

                        buffered_bbox = BoundingBox(
                            min_x=buffered_geom_wgs84.bounds[0],
                            min_y=buffered_geom_wgs84.bounds[1],
                            max_x=buffered_geom_wgs84.bounds[2],
                            max_y=buffered_geom_wgs84.bounds[3],
                        )
                except Exception as e:
                    logger.warning(
                        f"[{name}] Error computing buffered geometry in output CRS: {e}. "
                        "Using Web Mercator-buffered bounds."
                    )
                    buffered_bbox = aoi_obj.buffered_bounds(output_crs=output_crs)
            else:
                buffered_bbox = aoi_obj.bounds()

            all_tiles = source.search(buffered_bbox)

            _report("Filtering tiles by AOI intersection...")
            filtered_tiles = filter_tiles_by_aoi(all_tiles, aoi_obj, proj_bounds)

            _report("Prioritizing datasets...")
            prioritized_tiles = prioritize_datasets(filtered_tiles)

            if not prioritized_tiles:
                return PipelineResult(
                    name=name, status="failed", error="No tiles found after filtering"
                )

            _report("Validating coverage...")
            validate_coverage(prioritized_tiles, aoi_obj, proj_bounds, require_full_coverage)

            dataset_count = len(set(t.dataset_id for t in prioritized_tiles))
            _report(f"Found {len(prioritized_tiles)} tiles from {dataset_count} datasets")

            manifest = Manifest(aoi_obj, prioritized_tiles, buffer, cellsize)
            manifest_path = output_path.parent / f"{output_path.stem}.tif.manifest.json"
            manifest.save(manifest_path)
            _report(f"Saved manifest to {manifest_path}")

            if mode == "download-only":
                return PipelineResult(
                    name=name,
                    status="success",
                    manifest_path=manifest_path,
                    tile_count=len(prioritized_tiles),
                    dataset_count=dataset_count,
                )
        else:
            _report("Loading manifest for process-only mode...")
            manifest_path = output_path.parent / f"{output_path.stem}.tif.manifest.json"
            manifest = Manifest.load(manifest_path)
            prioritized_tiles = manifest.tiles
            aoi_obj = manifest.aoi
            _report(f"Loaded {len(prioritized_tiles)} tiles from manifest")

        dataset_count = len(set(t.dataset_id for t in prioritized_tiles))

        # Step 2: Download tiles
        if mode != "process-only":
            _report("Downloading tiles...")

            def _download_progress(completed, total):
                _report(f"Downloaded {completed}/{total} tiles")

            downloader = TileDownloader(data_dir=data_dir, progress_callback=_download_progress)
            downloaded_tiles = downloader.download(prioritized_tiles)
            _report(f"Downloaded {len(downloaded_tiles)} tiles")

            for tile in downloaded_tiles:
                for orig_tile in prioritized_tiles:
                    if orig_tile.id == tile.id:
                        orig_tile.local_path = tile.local_path
        else:
            downloaded_tiles = prioritized_tiles

        # Step 3: Create VRTs and mosaic
        _report("Mosaicking tiles...")
        with tempfile.TemporaryDirectory() as tmpdir:
            mosaicker = VRTMosaicker(Path(tmpdir))

            # Group tiles by (dataset_id, crs) to create separate VRTs for each projection
            from .crs import get_crs_from_raster, extract_utm_zone

            datasets_by_crs: dict[tuple[str, str], list[Tile]] = defaultdict(list)
            for tile in downloaded_tiles:
                # Get the actual CRS from the downloaded tile file
                tile_crs = get_crs_from_raster(str(tile.local_path))
                if tile_crs is None:
                    logger.warning(f"Could not detect CRS from {tile.id}, skipping grouping")
                    tile_crs = "UNKNOWN"
                datasets_by_crs[(tile.dataset_id, tile_crs)].append(tile)

            dataset_vrts = {}
            for (dataset_id, crs), tiles in datasets_by_crs.items():
                vrt_path = mosaicker.create_dataset_vrt(dataset_id, tiles)
                # Store with (dataset_id, crs) key for CRS-specific lookup
                dataset_vrts[(dataset_id, crs)] = vrt_path

            # Step 4: Merge (priority overwrite), reproject, clip, and optionally
            # snap to grid in a single gdalwarp pass. Combining these avoids
            # materializing multiple full-resolution intermediate rasters between
            # steps (previously merged.tif and reprojected.tif), which cuts both
            # runtime and disk usage significantly for large multi-dataset jobs.
            cutline_geometry_wgs84 = None
            if not no_clip:
                _report("Merging, reprojecting, and clipping to AOI with buffer...")

                clipping_geometry = aoi_obj.buffered_geometry_in_crs(output_crs)

                import geopandas as gpd

                gdf_clip = gpd.GeoDataFrame([{"geometry": clipping_geometry}], crs=output_crs)
                gdf_wgs84 = gdf_clip.to_crs("EPSG:4326")
                cutline_geometry_wgs84 = gdf_wgs84.iloc[0].geometry
            else:
                _report("Merging and reprojecting (clipping disabled)...")

            merger = DatasetMerger(Path(tmpdir))
            clipped = merger.merge_reproject_clip(
                downloaded_tiles,
                Path(tmpdir) / "merged.tif",
                target_crs=output_crs,
                dataset_vrts=dataset_vrts,
                cellsize=effective_cellsize,
                cutline_geometry=cutline_geometry_wgs84,
                snap_to_grid=not no_snap,
            )

            if not no_clip:
                _report("Merged, reprojected, and clipped")
            else:
                _report("Merged and reprojected (unclipped)")

            # Step 5: Convert elevation units and generate Cloud-Optimized GeoTIFF in one pass
            # Combines elevation conversion (meters → target CRS units) with COG generation
            # to avoid materializing an intermediate raster.
            _report("Converting elevation units and generating Cloud-Optimized GeoTIFF...")

            elevation_converter = ElevationConverter()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            elevation_converter.convert_and_generate_cog(
                clipped,
                output_path,
                target_crs=output_crs,
                generate_overviews=not no_overviews,
            )

        _report(f"SUCCESS! DEM saved to: {output_path}")

        return PipelineResult(
            name=name,
            status="success",
            output_path=output_path,
            manifest_path=manifest_path if mode != "process-only" else None,
            tile_count=len(prioritized_tiles),
            dataset_count=dataset_count,
        )

    except Exception as e:
        logger.error(f"[{name}] Pipeline failed: {e}", exc_info=True)
        return PipelineResult(name=name, status="failed", error=str(e))
