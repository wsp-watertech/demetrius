"""Command-line interface for demetrius."""

import logging
from pathlib import Path

import click

from .filtering import filter_tiles_by_aoi
from .inspector import InspectionReport
from .models import AOI
from .priority import prioritize_datasets
from .tnm import TNMTileSource

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
    help="Buffer distance in output CRS units for tile discovery and clipping (default: 0)",
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
    "--no-clip",
    is_flag=True,
    default=False,
    help="Disable clipping to AOI (output is full merged/reprojected extent)",
)
@click.option(
    "--project-bounds",
    required=True,
    type=click.Path(exists=True),
    help="Path to project boundaries (GeoParquet, GeoJSON, shapefile, etc.)",
)
@click.option(
    "--require-full-coverage",
    is_flag=True,
    default=False,
    help="Require full coverage of original AOI (default: coverage is optional)",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    help="Directory for downloaded tiles (default: DEMETRIUS_DATA_DIR env var or ~/.demetrius)",
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
    no_clip,
    project_bounds,
    require_full_coverage,
    data_dir,
    mode,
):
    """Process the DEM workflow.

    Run the full demetrius pipeline, or selected download-only or
    process-only stages, for the provided area of interest.
    """
    from .pipeline import run_pipeline

    result = run_pipeline(
        aoi,
        output,
        name=Path(output).stem,
        output_crs=output_crs,
        ffrd=ffrd,
        buffer=buffer,
        cellsize=cellsize,
        no_snap=no_snap,
        no_clip=no_clip,
        project_bounds=project_bounds,
        require_full_coverage=require_full_coverage,
        data_dir=data_dir,
        mode=mode,
        progress_cb=lambda msg: click.echo(msg),
    )

    if result.status == "failed":
        click.echo(f"\n✗ Error: {result.error}", err=True)
        raise click.Abort()

    if mode == "download-only":
        click.echo("\n✓ Download-only mode complete")
        return

    click.echo("\n" + "=" * 70)
    click.echo(f"✓ SUCCESS! DEM saved to: {result.output_path}")
    click.echo("=" * 70)


@cli.command()
@click.option(
    "--aoi",
    required=True,
    type=click.Path(exists=True),
    help="Path to AOI geometry",
)
def inspect(aoi: str) -> None:
    """Inspect available tiles for an AOI without downloading them."""
    try:
        from .coverage import validate_coverage

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
        validate_coverage(
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


@cli.command()
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True),
    help="Path to vector file of AOI polygons (GeoParquet, GeoJSON, shapefile, etc.)",
)
@click.option(
    "--name-field",
    required=True,
    help="Column in --input whose value should be used to name each output DEM "
    "(e.g. 'name'). No field name is assumed; you must specify one.",
)
@click.option(
    "--output-dir",
    default="./batch_output",
    type=click.Path(),
    help="Output directory for generated DEMs and manifests (default: ./batch_output)",
)
@click.option(
    "--output-crs",
    help="Target CRS (e.g., EPSG:32618)",
)
@click.option(
    "--ffrd",
    is_flag=True,
    default=False,
    help="Use FFRD custom projection for every AOI (overrides --output-crs, "
    "enforces snapping, defaults to cellsize=4)",
)
@click.option(
    "--buffer",
    default=0,
    type=float,
    help="Buffer distance in output CRS units for tile discovery and clipping (default: 0)",
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
    "--no-clip",
    is_flag=True,
    default=False,
    help="Disable clipping to AOI (output is full merged/reprojected extent)",
)
@click.option(
    "--project-bounds",
    type=click.Path(exists=True),
    help="Path to project boundaries (GeoParquet, GeoJSON, shapefile, etc.)",
)
@click.option(
    "--require-full-coverage",
    is_flag=True,
    default=False,
    help="Require full coverage of each AOI (default: coverage is optional)",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    help="Directory for downloaded tiles (default: DEMETRIUS_DATA_DIR env var or ~/.demetrius)",
)
@click.option(
    "--mode",
    default="full",
    type=click.Choice(["full", "download-only", "process-only"]),
    help="Processing mode",
)
@click.option(
    "--max-workers",
    default=1,
    type=int,
    help="Number of AOIs to process concurrently (default: 1, sequential)",
)
def batch(
    input_path,
    name_field,
    output_dir,
    output_crs,
    ffrd,
    buffer,
    cellsize,
    no_snap,
    no_clip,
    project_bounds,
    require_full_coverage,
    data_dir,
    mode,
    max_workers,
):
    """Batch-process DEMs for every AOI polygon in an input file.

    Each AOI is named using a user-specified column from the input file.
    """
    from .batch import batch_process

    try:
        results = batch_process(
            input_path,
            name_field,
            output_dir,
            output_crs=output_crs,
            ffrd=ffrd,
            buffer=buffer,
            cellsize=cellsize,
            no_snap=no_snap,
            no_clip=no_clip,
            project_bounds=project_bounds,
            require_full_coverage=require_full_coverage,
            data_dir=data_dir,
            mode=mode,
            max_workers=max_workers,
        )
    except ValueError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()

    successful = [r for r in results if r.status == "success"]
    failed = [r for r in results if r.status == "failed"]

    click.echo("\n" + "=" * 70)
    click.echo("BATCH PROCESSING SUMMARY")
    click.echo("=" * 70)
    click.echo(f"Processed: {len(results)} AOIs")
    click.echo(f"Successful: {len(successful)}")
    click.echo(f"Failed: {len(failed)}")

    if successful:
        click.echo("\nSuccessful:")
        for r in successful:
            click.echo(
                f"  {r.name:20s} | Tiles: {r.tile_count:3d} | "
                f"Datasets: {r.dataset_count:2d} | Output: {r.output_path}"
            )

    if failed:
        click.echo("\nFailed:")
        for r in failed:
            click.echo(f"  {r.name:20s} | Error: {r.error}")

    click.echo("=" * 70)

    if failed:
        raise click.Abort()


if __name__ == "__main__":
    cli()
