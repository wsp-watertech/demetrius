"""Batch DEM processing across multiple AOI polygons.

Runs :func:`demetrius.pipeline.run_pipeline` once per row of a GeoDataFrame,
naming each output using a user-specified field. Unlike a one-off script,
this is a reusable library entry point that the CLI's ``batch`` command and
other code can call directly.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd

from .models import AOI
from .pipeline import PipelineResult, run_pipeline
from .project_boundaries import ProjectBoundaries

logger = logging.getLogger(__name__)


def _load_geodataframe(source: Union[str, Path, gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    """Load a GeoDataFrame from a path, or pass one through unchanged.

    Parameters
    ----------
    source : str | Path | gpd.GeoDataFrame
        Path to a vector file (parquet, GeoJSON, shapefile, etc.), or an
        already-loaded GeoDataFrame.

    Returns
    -------
    gpd.GeoDataFrame
        The loaded (or passed-through) GeoDataFrame.
    """
    if isinstance(source, gpd.GeoDataFrame):
        return source

    path = Path(source)
    if path.suffix.lower() == ".parquet":
        return gpd.read_parquet(path)
    return gpd.read_file(path)


def batch_process(
    source: Union[str, Path, gpd.GeoDataFrame],
    name_field: str,
    output_dir: Union[str, Path],
    *,
    output_crs: Optional[str] = None,
    ffrd: bool = False,
    buffer: float = 0,
    cellsize: Optional[float] = None,
    no_snap: bool = False,
    no_clip: bool = False,
    project_bounds: Optional[Union[ProjectBoundaries, str, Path]] = None,
    require_full_coverage: bool = False,
    mode: str = "full",
    max_workers: int = 1,
) -> list[PipelineResult]:
    """Run the DEM pipeline for every AOI polygon in a vector file/GeoDataFrame.

    Parameters
    ----------
    source : str | Path | gpd.GeoDataFrame
        Path to a vector file containing AOI polygons, or an already-loaded
        GeoDataFrame.
    name_field : str
        Name of the column in ``source`` to use for naming each output DEM
        (e.g. ``"name"``, ``"shortname"``, ``"huc10"``). Must be present in
        the input; no field name is assumed.
    output_dir : str | Path
        Directory in which output COGs and manifests are written. Each AOI's
        output is named ``{output_dir}/{name}.tif``.
    output_crs : str | None
        Target CRS (e.g. ``"EPSG:32618"``). Ignored if ``ffrd=True``.
    ffrd : bool, default=False
        Use the bundled FFRD projection for every AOI (see
        :func:`demetrius.pipeline.run_pipeline`).
    buffer : float, default=0
        Buffer distance in output CRS units for tile discovery and clipping.
    cellsize : float | None
        Output cellsize in target CRS units.
    no_snap : bool, default=False
        Disable grid snapping.
    no_clip : bool, default=False
        Disable clipping to AOI. If True, output is full merged/reprojected extent
        for every AOI rather than clipped to the buffered geometry.
    project_bounds : ProjectBoundaries | str | Path | None
        Project boundaries instance, or a path to load one from. Loaded once
        and reused across all AOIs.
    require_full_coverage : bool, default=False
        Require full coverage of each AOI by project boundaries.
    mode : {"full", "download-only", "process-only"}
        Which pipeline stages to run for each AOI.
    max_workers : int, default=1
        Number of AOIs to process concurrently. AOIs are independent, so
        values >1 can substantially speed up large batches; keep at 1 if the
        underlying GDAL toolchain is not safe to run concurrently in your
        environment.

    Returns
    -------
    list[PipelineResult]
        One result per input AOI, in input row order.

    Raises
    ------
    ValueError
        If ``name_field`` is not a column in the input data.
    """
    gdf = _load_geodataframe(source)

    if name_field not in gdf.columns:
        raise ValueError(
            f"name_field {name_field!r} not found in input columns: {list(gdf.columns)}"
        )

    # AOI expects WGS84 geometry, matching AOI.from_file's behavior.
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load project bounds once and reuse across all AOIs, rather than
    # re-reading the file for every row.
    if project_bounds is not None and not isinstance(project_bounds, ProjectBoundaries):
        project_bounds = ProjectBoundaries.from_file(str(project_bounds))

    def _run_one(idx: int, row) -> PipelineResult:
        name = str(row[name_field])
        output_path = output_dir / f"{name}.tif"
        aoi = AOI(geometry=row.geometry, buffer=int(buffer))
        logger.info(f"[{idx}] Starting {name}...")
        return run_pipeline(
            aoi,
            output_path,
            name=name,
            output_crs=output_crs,
            ffrd=ffrd,
            buffer=int(buffer),
            cellsize=cellsize,
            no_snap=no_snap,
            no_clip=no_clip,
            project_bounds=project_bounds,
            require_full_coverage=require_full_coverage,
            mode=mode,
        )

    results: list[Optional[PipelineResult]] = [None] * len(gdf)

    if max_workers <= 1:
        for idx, (_, row) in enumerate(gdf.iterrows()):
            results[idx] = _run_one(idx, row)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_run_one, idx, row): idx
                for idx, (_, row) in enumerate(gdf.iterrows())
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

    return results  # type: ignore[return-value]
