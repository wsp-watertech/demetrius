"""Command-line interface for demetrius."""

import logging
from pathlib import Path
from collections import defaultdict
import tempfile

import click

from .coverage import validate_coverage
from .downloader import TileDownloader
from .elevation_converter import ElevationConverter
from .filtering import filter_tiles_by_aoi
from .inspector import InspectionReport
from .manifest import Manifest
from .models import AOI, BoundingBox
from .priority import prioritize_datasets
from .tnm import TNMTileSource
from .crs import get_target_utm_for_tiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


@click.group()
def cli():
    """demetrius: High-resolution DEM assembly from USGS 3DEP data."""
    pass


@cli.command()
@click.option(
    "--aoi",
    required=True,
    type=click.Path(exists=True),
    help="Path to AOI geometry (GeoJSON, shapefile, or GeoPackage)",
)
@click.option(
    "--output",
    default="dem.tif",
    type=click.Path(),
    help="Output COG path",
)
@click.option(
    "--output-crs",
    help="Target CRS (e.g., EPSG:32618)",
)
@click.option(
    "--ffrd",
    is_flag=True,
    default=False,
    help="Use FFRD custom projection (overrides --output-crs, enforces snapping, defaults to cellsize=4)",
)
@click.option(
    "--buffer",
    default=0,
    type=int,
    help="Buffer distance in meters for tile discovery",
)
@click.option(
    "--cellsize",
    default=None,
    type=float,
    help="Output cellsize in target CRS units (optional)",
)
@click.option(
    "--no-snap",
    is_flag=True,
    default=False,
    help="Disable grid snapping (default: enabled)",
)
@click.option(
    "--project-bounds",
    required=True,
    type=click.Path(exists=True),
    help="Path to project boundaries (GeoParquet, GeoJSON, shapefile, etc.)",
)
@click.option(
    "--require-full-coverage",
    default=False,
    type=bool,
    help="Require full coverage of original AOI",
)
@click.option(
    "--mode",
    default="full",
    type=click.Choice(["full", "download-only", "process-only"]),
    help="Processing mode",
)
def process(
    aoi,
    output,
    output_crs,
    ffrd,
    buffer,
    cellsize,
    no_snap,
    project_bounds,
    require_full_coverage,
    mode,
):
    """Process the DEM workflow.

    Run the full demetrius pipeline, or selected download-only or
    process-only stages, for the provided area of interest.

    Parameters
    ----------
    aoi : str
        Path to the AOI geometry file.
    output : str
        Output path for the generated Cloud-Optimized GeoTIFF.
    output_crs : str | None
        Target coordinate reference system for the output raster.
    ffrd : bool
        Whether to use the bundled FFRD projection and snapping defaults.
    buffer : int
        Buffer distance, in meters, used for tile discovery.
    cellsize : float | None
        Output cell size in target CRS units.
    no_snap : bool
        Whether grid snapping should be skipped.
    project_bounds : str
        Path to the project boundaries dataset.
    require_full_coverage : bool
        Whether the original AOI must be fully covered by the selected data.
    mode : str
        Processing mode to run: ``full``, ``download-only``, or
        ``process-only``.

    Returns
    -------
    None
        This command writes output files and reports progress to the CLI.
    """
    try:
        from .cog import COGGenerator
        from .clipper import Clipper
        from .crs import get_target_utm_for_tiles
        from .downloader import TileDownloader
        from .merger import DatasetMerger
        from .mosaicker import VRTMosaicker
        from .reprojector import Reprojector
        from .project_boundaries import ProjectBoundaries
        from .snapper import Snapper
        from .projections import get_projection_file
        import tempfile
        from collections import defaultdict

        # Handle --ffrd flag
        if ffrd:
            if output_crs:
                click.echo("⚠ --ffrd overrides --output-crs")
            ffrd_path = get_projection_file("ffrd.prj")
            if not ffrd_path:
                raise RuntimeError("FFRD projection file not found")

            # Load WKT from FFRD projection file
            ffrd_wkt = ffrd_path.read_text().strip()
            if not ffrd_wkt:
                raise ValueError("FFRD projection file is empty")

            output_crs = ffrd_wkt
            click.echo(f"Using FFRD custom projection")

            # Force snapping when using --ffrd
            if no_snap:
                click.echo("⚠ --ffrd requires snapping (--no-snap ignored)")
            no_snap = False

            # Use cellsize=4 unless explicitly specified
            if cellsize is None:
                cellsize = 4.0
                click.echo("Using FFRD default cellsize: 4")

        # Load project boundaries
        click.echo(f"Loading project boundaries from {project_bounds}")
        proj_bounds = ProjectBoundaries.from_file(project_bounds)
        click.echo(f"✓ Loaded project boundaries")

        # Load AOI
        click.echo(f"Loading AOI from {aoi}")
        aoi_obj = AOI.from_file(aoi, buffer=buffer)
        click.echo(f"✓ Loaded AOI")

        # Determine target CRS early (needed for buffering and cellsize conversion)
        if not output_crs:
            click.echo("\n[0/5] Auto-detecting target CRS...")
            source = TNMTileSource()
            # Search with unbuffered bounds to find tiles for CRS detection
            initial_bbox = aoi_obj.bounds()
            initial_tiles = source.search(initial_bbox)
            if initial_tiles:
                output_crs = get_target_utm_for_tiles(initial_tiles)
                click.echo(f"Auto-selected target CRS: {output_crs}")
            else:
                raise ValueError("No tiles found for CRS auto-detection")
        else:
            click.echo(f"Using target CRS: {output_crs}")

        # Compute default cellsize if not specified
        # Default is 1 meter converted to the units of output_crs
        effective_cellsize = cellsize
        if cellsize is None:
            snapper = Snapper()
            effective_cellsize = snapper.get_conversion_factor_for_snapping(output_crs)
            if no_snap:
                click.echo(
                    f"Using default cellsize (snapping disabled): 1m in {output_crs} units = {effective_cellsize:.9f}"
                )
            else:
                click.echo(
                    f"Using default cellsize (snapping enabled): 1m in {output_crs} units = {effective_cellsize:.9f}"
                )

        # Step 1: Query TNM
        if mode != "process-only":
            click.echo("\n[1/5] Querying TNM for tiles...")
            source = TNMTileSource()

            # Get buffered bounds in output CRS
            if buffer > 0:
                try:
                    buffered_geom_in_output_crs = aoi_obj.buffered_geometry_in_crs(output_crs)
                    # Check if buffering was successful (geometry should be valid and in expected CRS)
                    bounds = buffered_geom_in_output_crs.bounds
                    if any(b == float("inf") or b == float("-inf") for b in bounds):
                        # Buffering fell back to Web Mercator, geometry is in WGS84
                        logger.info(
                            "Buffering fell back to Web Mercator, using Web Mercator-buffered bounds"
                        )
                        buffered_bbox = aoi_obj.buffered_bounds()
                    else:
                        # Buffering succeeded in output CRS, reproject back to WGS84 for TNM search
                        import geopandas as gpd

                        gdf = gpd.GeoDataFrame(
                            [{"geometry": buffered_geom_in_output_crs}], crs=output_crs
                        )
                        gdf_wgs84 = gdf.to_crs("EPSG:4326")
                        buffered_geom_wgs84 = gdf_wgs84.iloc[0].geometry
                        buffered_bbox = BoundingBox(
                            min_x=buffered_geom_wgs84.bounds[0],
                            min_y=buffered_geom_wgs84.bounds[1],
                            max_x=buffered_geom_wgs84.bounds[2],
                            max_y=buffered_geom_wgs84.bounds[3],
                        )
                except Exception as e:
                    logger.warning(
                        f"Error computing buffered geometry in output CRS: {e}. Using Web Mercator-buffered bounds."
                    )
                    buffered_bbox = aoi_obj.buffered_bounds()
            else:
                buffered_bbox = aoi_obj.bounds()

            all_tiles = source.search(buffered_bbox)

            # Filter by AOI with project boundaries
            click.echo("Filtering tiles by AOI intersection...")
            filtered_tiles = filter_tiles_by_aoi(all_tiles, aoi_obj, proj_bounds)

            # Prioritize datasets
            click.echo("Prioritizing datasets...")
            prioritized_tiles = prioritize_datasets(filtered_tiles)

            # Validate coverage with project boundaries
            click.echo("Validating coverage...")
            validate_coverage(prioritized_tiles, aoi_obj, proj_bounds, require_full_coverage)

            click.echo(
                f"✓ Found {len(prioritized_tiles)} tiles from {len(set(t.dataset_id for t in prioritized_tiles))} datasets"
            )

            # Create manifest
            manifest = Manifest(aoi_obj, prioritized_tiles, buffer, cellsize)
            output_stem = Path(output).stem
            manifest_path = Path(output).parent / f"{output_stem}.tif.manifest.json"
            manifest.save(manifest_path)
            click.echo(f"✓ Saved manifest to {manifest_path}")

            if mode == "download-only":
                click.echo("\n✓ Download-only mode complete")
                return

        else:
            # Load manifest for process-only mode
            click.echo("Loading manifest for process-only mode...")
            output_stem = Path(output).stem
            manifest_path = Path(output).parent / f"{output_stem}.tif.manifest.json"
            manifest = Manifest.load(manifest_path)
            prioritized_tiles = manifest.tiles
            aoi_obj = manifest.aoi
            click.echo(f"✓ Loaded {len(prioritized_tiles)} tiles from manifest")

        # Step 2: Download tiles
        if mode != "process-only":
            click.echo("\n[2/5] Downloading tiles...")

            def progress_cb(completed, total):
                click.echo(f"  Downloaded {completed}/{total} tiles", nl=False)
                click.echo("\r", nl=False)

            downloader = TileDownloader(progress_callback=progress_cb)
            downloaded_tiles = downloader.download(prioritized_tiles)
            click.echo(f"\n✓ Downloaded {len(downloaded_tiles)} tiles")

            # Update tiles with local paths
            for tile in downloaded_tiles:
                for orig_tile in prioritized_tiles:
                    if orig_tile.id == tile.id:
                        orig_tile.local_path = tile.local_path
        else:
            downloaded_tiles = prioritized_tiles

        # Step 3: Create VRTs and mosaic
        click.echo(f"\n[3/5] Mosaicking tiles...")
        with tempfile.TemporaryDirectory() as tmpdir:
            mosaicker = VRTMosaicker(Path(tmpdir))

            # Group by dataset
            datasets = defaultdict(list)
            for tile in downloaded_tiles:
                datasets[tile.dataset_id].append(tile)

            # Create VRT per dataset
            dataset_vrts = {}
            for dataset_id, tiles in datasets.items():
                vrt_path = mosaicker.create_dataset_vrt(dataset_id, tiles)
                dataset_vrts[dataset_id] = vrt_path

            # Merge datasets
            click.echo("Merging datasets...")
            if len(dataset_vrts) == 1:
                merged_raster = list(dataset_vrts.values())[0]
            else:
                merger = DatasetMerger(Path(tmpdir))
                merged_raster = merger.merge_datasets(
                    downloaded_tiles,
                    Path(tmpdir) / "merged.tif",
                    dataset_vrts=dataset_vrts,
                )

            # Step 4: Reproject (if needed) and clip
            click.echo(f"\n[4/5] Reprojecting and clipping...")

            # Reproject
            reprojector = Reprojector()
            reprojected = Path(tmpdir) / "reprojected.tif"
            reprojector.reproject(
                merged_raster, reprojected, output_crs, cellsize=effective_cellsize
            )

            # Clip
            click.echo("Clipping to AOI with buffer...")

            # Get buffered geometry in output CRS for accurate meter-based buffering
            clipping_geometry = aoi_obj.buffered_geometry_in_crs(output_crs)

            # Reproject back to WGS84 for gdalwarp
            import geopandas as gpd

            gdf_clip = gpd.GeoDataFrame([{"geometry": clipping_geometry}], crs=output_crs)
            gdf_wgs84 = gdf_clip.to_crs("EPSG:4326")
            clipping_geometry_wgs84 = gdf_wgs84.iloc[0].geometry

            clipper = Clipper()
            clipped = Path(tmpdir) / "clipped.tif"
            clipper.clip(
                reprojected,
                clipped,
                clipping_geometry_wgs84,
                geometry_crs="EPSG:4326",
            )
            click.echo(f"✓ Reprojected and clipped")

            # Step 5: Snap to grid (optional)
            if not no_snap:
                click.echo(f"\n[5/5] Snapping to grid and converting elevation units...")

                snapped = Path(tmpdir) / "snapped.tif"
                # Use effective_cellsize which is 1m converted to output_crs units
                # (or explicit cellsize if user provided one)
                snapper = Snapper()
                snapper.snap(clipped, snapped, cellsize=effective_cellsize)

                # Use snapped file for elevation conversion
                clipped = snapped
            else:
                click.echo(
                    f"\n[5/5] Converting elevation units and generating Cloud-Optimized GeoTIFF..."
                )

            # Convert elevation units if necessary
            elevation_converter = ElevationConverter()
            converted = Path(tmpdir) / "converted.tif"
            elevation_converter.convert(clipped, converted, output_crs)

            # Generate COG from converted raster
            cog_gen = COGGenerator()
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            cog_gen.generate(converted, output_path)

        click.echo(f"\n" + "=" * 70)
        click.echo(f"✓ SUCCESS! DEM saved to: {output_path}")
        click.echo(f"=" * 70)

    except Exception as e:
        click.echo(f"\n✗ Error: {e}", err=True)
        raise click.Abort()


@cli.command()
@click.option(
    "--aoi",
    required=True,
    type=click.Path(exists=True),
    help="Path to AOI geometry",
)
def inspect(aoi: str) -> None:
    """Inspect available tiles for an AOI without downloading them.

    Parameters
    ----------
    aoi : str
        Path to the AOI geometry file.

    Returns
    -------
    None
        This command prints a tile inspection summary to the CLI.
    """
    try:
        # Load AOI
        aoi_obj = AOI.from_file(aoi)
        click.echo(f"✓ Loaded AOI from {aoi}")

        # Query TNM
        click.echo("Querying TNM for tiles...")
        source = TNMTileSource()
        buffered_bbox = aoi_obj.buffered_bounds()
        all_tiles = source.search(buffered_bbox)

        # Filter by AOI
        click.echo("Filtering tiles by AOI intersection...")
        filtered_tiles = filter_tiles_by_aoi(all_tiles, aoi_obj)

        # Prioritize datasets
        click.echo("Prioritizing datasets...")
        prioritized_tiles = prioritize_datasets(filtered_tiles)

        # Validate coverage
        click.echo("Validating coverage...")
        coverage = validate_coverage(
            prioritized_tiles,
            aoi_obj,
            require_full_coverage=False,
        )

        # Generate report
        report = InspectionReport(prioritized_tiles, aoi_obj)
        click.echo("")
        click.echo(report.summary())

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()


if __name__ == "__main__":
    cli()
