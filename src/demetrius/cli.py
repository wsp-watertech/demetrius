"""Command-line interface for demetrius."""

import logging
from pathlib import Path
from collections import defaultdict
import tempfile

import click

from .coverage import validate_coverage
from .downloader import TileDownloader
from .filtering import filter_tiles_by_aoi
from .inspector import InspectionReport
from .manifest import Manifest
from .models import AOI
from .priority import prioritize_datasets
from .tnm import TNMTileSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@click.group()
def cli():
    """demetrius - High-resolution DEM assembly from USGS 3DEP data."""
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
    "--buffer-distance",
    default=1000,
    type=int,
    help="Buffer distance in meters for tile discovery",
)
@click.option(
    "--require-full-coverage",
    default=True,
    type=bool,
    help="Require full coverage of original AOI",
)
@click.option(
    "--mode",
    default="full",
    type=click.Choice(["full", "download-only", "process-only"]),
    help="Processing mode",
)
def process(aoi, output, output_crs, buffer_distance, require_full_coverage, mode):
    """Process DEM workflow (default: full pipeline)."""
    try:
        from .cog import COGGenerator
        from .clipper import Clipper
        from .crs import get_target_utm_for_tiles
        from .downloader import TileDownloader
        from .merger import DatasetMerger
        from .mosaicker import VRTMosaicker
        from .reprojector import Reprojector
        import tempfile
        from collections import defaultdict

        # Load AOI
        click.echo(f"Loading AOI from {aoi}")
        aoi_obj = AOI.from_file(aoi, buffer_distance=buffer_distance)
        click.echo(f"✓ Loaded AOI")

        # Step 1: Query TNM
        if mode != "process-only":
            click.echo("\n[1/5] Querying TNM for tiles...")
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
            validate_coverage(prioritized_tiles, aoi_obj, require_full_coverage)

            click.echo(f"✓ Found {len(prioritized_tiles)} tiles from {len(set(t.dataset_id for t in prioritized_tiles))} datasets")

            # Create manifest
            manifest = Manifest(aoi_obj, prioritized_tiles, buffer_distance)
            manifest_path = Path(output).parent / "manifest.json"
            manifest.save(manifest_path)
            click.echo(f"✓ Saved manifest to {manifest_path}")

            if mode == "download-only":
                click.echo("\n✓ Download-only mode complete")
                return

        else:
            # Load manifest for process-only mode
            click.echo("Loading manifest for process-only mode...")
            manifest_path = Path(output).parent / "manifest.json"
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
                merged_raster = merger.merge_datasets(downloaded_tiles, Path(tmpdir) / "merged.tif")

            # Step 4: Reproject (if needed) and clip
            click.echo(f"\n[4/5] Reprojecting and clipping...")

            # Determine target CRS
            if not output_crs:
                output_crs = get_target_utm_for_tiles(downloaded_tiles)
                click.echo(f"Auto-selected target CRS: {output_crs}")
            else:
                click.echo(f"Using target CRS: {output_crs}")

            # Reproject
            reprojector = Reprojector()
            reprojected = Path(tmpdir) / "reprojected.tif"
            reprojector.reproject(merged_raster, reprojected, output_crs)

            # Clip
            clipper = Clipper()
            clipped = Path(tmpdir) / "clipped.tif"
            clipper.clip(reprojected, clipped, aoi_obj.geometry)
            click.echo(f"✓ Reprojected and clipped")

            # Step 5: Generate COG
            click.echo(f"\n[5/5] Generating Cloud-Optimized GeoTIFF...")
            cog_gen = COGGenerator()
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            cog_gen.generate(clipped, output_path)

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
    """Inspect tiles for an AOI without downloading."""
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
