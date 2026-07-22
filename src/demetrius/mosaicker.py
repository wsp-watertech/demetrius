"""VRT-based mosaicking for efficient large-scale raster merging."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Sequence

from .models import Tile

logger = logging.getLogger(__name__)


def materialize_vrt(vrt_path: Path, output_path: Path) -> Path:
    """Flatten a VRT mosaic into a single materialized GeoTIFF.

    Multi-tile VRTs are cheap to build but expensive to read repeatedly:
    every block read must resolve and open whichever underlying tile
    file(s) intersect that block. Downstream operations (gdalwarp merges,
    reprojection) that touch such a VRT pay that cost across its full
    extent. Materializing once, up front, turns later reads into a single
    well-organized file instead of many small ones.

    Parameters
    ----------
    vrt_path : Path
        Path to a raster, either a ``.vrt`` mosaic of many tiles or an
        already-materialized single file.
    output_path : Path
        Destination path for the materialized GeoTIFF. Ignored if
        ``vrt_path`` is not a VRT.

    Returns
    -------
    Path
        Path to a materialized (non-VRT) raster. If ``vrt_path`` was
        already a real raster file (e.g. a single tile), it is returned
        unchanged.

    Raises
    ------
    ValueError
        If materialization fails.
    RuntimeError
        If ``gdal_translate`` is not available.
    """
    vrt_path = Path(vrt_path)
    if vrt_path.suffix.lower() != ".vrt":
        return vrt_path

    logger.info(f"Materializing VRT {vrt_path} to {output_path}")

    try:
        cmd = [
            "gdal_translate",
            "-co",
            "TILED=YES",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "BIGTIFF=IF_SAFER",
            str(vrt_path),
            str(output_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )

        if result.returncode != 0:
            raise ValueError(f"gdal_translate materialization failed: {result.stderr}")

        logger.debug(f"Materialized: {output_path}")
        return output_path

    except FileNotFoundError:
        raise RuntimeError("gdal_translate not found. Please install GDAL command-line tools.")


class VRTMosaicker:
    """Create Virtual Raster (VRT) files for efficient tile mosaicking."""

    def __init__(self, working_dir: Path):
        """Initialize mosaicker.

        Parameters
        ----------
        working_dir : Path
            Directory for VRT files.

        Returns
        -------
        None
            Initializes the mosaicker instance.
        """
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def create_dataset_vrt(self, dataset_id: str, tiles: Sequence[Tile]) -> Path:
        """Create a VRT file for all tiles in a dataset.

        Uses gdalbuildvrt for efficient virtual mosaicking without
        loading data into memory.

        Parameters
        ----------
        dataset_id : str
            Dataset identifier.
        tiles : Sequence[Tile]
            Tiles in this dataset. All must have ``local_path`` set.

        Returns
        -------
        Path
            Path to the created VRT file.

        Raises
        ------
        ValueError
            If VRT creation fails.
        RuntimeError
            If ``gdalbuildvrt`` is not available.
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
        logger.debug(tiles)

        try:
            cmd = ["gdalbuildvrt", str(vrt_path)] + tile_paths
            logger.debug(f"Running command: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                raise ValueError(f"gdalbuildvrt failed: {result.stderr}")

            logger.debug(f"Created VRT: {vrt_path}")
            return vrt_path

        except FileNotFoundError:
            raise RuntimeError("gdalbuildvrt not found. Please install GDAL command-line tools.")

    def create_merged_vrt(
        self,
        dataset_vrts: dict[str, Path],
        output_vrt: Path,
    ) -> Path:
        """Merge multiple dataset VRTs with priority.

        Creates a layered VRT that respects dataset priority (newer overwrites older).

        Parameters
        ----------
        dataset_vrts : dict[str, Path]
            Mapping of ``dataset_id`` to VRT file path.
        output_vrt : Path
            Path for merged VRT output.

        Returns
        -------
        Path
            Path to the merged VRT.

        Raises
        ------
        ValueError
            If the merge fails.
        RuntimeError
            If ``gdalbuildvrt`` is not available.
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
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                raise ValueError(f"VRT merge failed: {result.stderr}")

            logger.debug(f"Created merged VRT: {output_vrt}")
            return output_vrt

        except FileNotFoundError:
            raise RuntimeError("gdalbuildvrt not found. Please install GDAL command-line tools.")
