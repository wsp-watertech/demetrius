"""Parallel tile downloading with integrity checks."""

import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional, Sequence

import httpx

from .models import Tile

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(os.environ.get("DEMETRIUS_DATA_DIR", Path.home() / ".demetrius"))
MAX_RETRIES = 3
RETRY_BACKOFF = 2


class TileDownloader:
    """Download tiles from TNM with parallel execution and retry logic."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        max_workers: int = 4,
        timeout: float = 60.0,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ):
        """Initialize downloader.

        Parameters
        ----------
        data_dir : Path | None, optional
            Base directory for downloads. Defaults to ``~/.demetrius``.
        max_workers : int, default=4
            Number of parallel download threads.
        timeout : float, default=60.0
            Request timeout in seconds.
        progress_callback : Callable[[int, int], None] | None, optional
            Optional callback receiving completed and total download counts.

        Returns
        -------
        None
            Initializes the downloader instance.
        """
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.max_workers = max_workers
        self.timeout = timeout
        self.progress_callback = progress_callback

    def download(self, tiles: Sequence[Tile]) -> list[Tile]:
        """Download all tiles in parallel.

        Parameters
        ----------
        tiles : Sequence[Tile]
            Tiles to download.

        Returns
        -------
        list[Tile]
            Tiles with ``local_path`` populated.

        Raises
        ------
        ValueError
            If any download fails after retries.
        """
        logger.info(f"Preparing to download {len(tiles)} tiles to {self.data_dir}")

        # Create directory structure
        self.data_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        failed = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._download_single, tile): tile for tile in tiles}

            completed_count = 0
            for future in as_completed(futures):
                tile = futures[future]
                try:
                    downloaded_tile = future.result()
                    downloaded.append(downloaded_tile)
                    completed_count += 1
                except Exception as e:
                    logger.error(f"Failed to download tile {tile.id}: {e}")
                    failed.append(tile)
                    completed_count += 1

                if self.progress_callback:
                    self.progress_callback(completed_count, len(tiles))

        if failed:
            raise ValueError(
                f"Failed to download {len(failed)} tile(s): {', '.join(t.id for t in failed)}"
            )

        logger.info(f"✓ Downloaded {len(downloaded)} tiles")
        
        # Validate tiles for readability (catch codec issues early)
        logger.info("Validating tile codecs...")
        validated = self._validate_tiles(downloaded)
        
        if validated:
            logger.info(f"✓ Validated {len(validated)} tiles")
        
        return validated

    def _download_single(self, tile: Tile, attempt: int = 1) -> Tile:
        """Download a single tile with retry logic.

        Parameters
        ----------
        tile : Tile
            Tile to download.
        attempt : int, default=1
            Current attempt number.

        Returns
        -------
        Tile
            Tile with ``local_path`` populated.

        Raises
        ------
        ValueError
            If the download fails after all retry attempts.
        """
        local_path = self._get_local_path(tile)

        # Skip if already exists
        if local_path.exists():
            logger.debug(f"Tile {tile.id} already exists, skipping download")
            tile.local_path = str(local_path)
            return tile

        try:
            logger.debug(f"Downloading tile {tile.id} from {tile.download_url}")

            with httpx.stream("GET", tile.download_url, timeout=self.timeout) as response:
                response.raise_for_status()

                # Create parent directory
                local_path.parent.mkdir(parents=True, exist_ok=True)

                # Download to temporary file first
                temp_path = local_path.with_suffix(".tmp")
                with open(temp_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)

                # Move to final location
                shutil.move(str(temp_path), str(local_path))

            logger.debug(f"Downloaded tile {tile.id} to {local_path}")
            tile.local_path = str(local_path)
            return tile

        except Exception as e:
            if attempt < MAX_RETRIES:
                logger.warning(f"Attempt {attempt} failed for tile {tile.id}, retrying... ({e})")
                import time

                time.sleep(RETRY_BACKOFF**attempt)
                return self._download_single(tile, attempt + 1)
            else:
                raise ValueError(f"Failed to download {tile.id} after {MAX_RETRIES} attempts: {e}")

    def _get_local_path(self, tile: Tile) -> Path:
        """Get local file path for tile.

        Parameters
        ----------
        tile : Tile
            Tile to get the local path for.

        Returns
        -------
        Path
            Local filesystem path for the tile.
        """
        dataset_dir = self.data_dir / f"dataset_{tile.dataset_id}"
        filename = f"tile_{tile.tile_id}.tif"
        return dataset_dir / filename

    def _validate_tiles(self, tiles: Sequence[Tile]) -> list[Tile]:
        """Validate tiles for readability and re-encode if codec issues detected.

        Some TNM tiles use unsupported TIFF compression codecs (e.g., ZSTD).
        This method tests each tile with gdalinfo and re-encodes with DEFLATE
        if codec errors are detected, ensuring all tiles are readable by gdalwarp.

        Parameters
        ----------
        tiles : Sequence[Tile]
            Downloaded tiles to validate.

        Returns
        -------
        list[Tile]
            Tiles that are readable or were successfully re-encoded.
        """
        valid_tiles = []
        invalid_tiles = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._validate_single, tile): tile for tile in tiles}

            for future in as_completed(futures):
                tile = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        valid_tiles.append(result)
                except Exception as e:
                    logger.error(f"Tile {tile.id} could not be validated or re-encoded: {e}")
                    invalid_tiles.append(tile)

        if invalid_tiles:
            logger.warning(
                f"Failed to validate/re-encode {len(invalid_tiles)} tile(s): "
                f"{', '.join(t.id for t in invalid_tiles)}. "
                f"These tiles will be excluded from processing."
            )

        return valid_tiles

    def _validate_single(self, tile: Tile) -> Optional[Tile]:
        """Validate a single tile and re-encode if codec issues detected.

        Parameters
        ----------
        tile : Tile
            Tile to validate.

        Returns
        -------
        Tile | None
            The tile if valid or successfully re-encoded, None if unable to repair.
        """
        local_path = Path(tile.local_path)

        # Test with gdalinfo to check for codec issues
        try:
            result = subprocess.run(
                ["gdalinfo", "-checksum", str(local_path)],
                capture_output=True,
                timeout=10,
                text=True,
            )

            # Look for codec errors in stderr
            if "Using code not yet in table" in result.stderr or "TIFFReadEncodedTile" in result.stderr:
                logger.warning(
                    f"Tile {tile.id} has unsupported TIFF codec, attempting re-encode to DEFLATE..."
                )
                return self._reencode_tile(tile, local_path)

            if result.returncode != 0:
                logger.warning(
                    f"gdalinfo returned error for {tile.id}: {result.stderr[:200]}"
                )
                # Try to re-encode even on other errors
                return self._reencode_tile(tile, local_path)

            logger.debug(f"Tile {tile.id} is readable")
            return tile

        except subprocess.TimeoutExpired:
            logger.warning(f"gdalinfo timeout for {tile.id}, attempting re-encode")
            return self._reencode_tile(tile, local_path)
        except Exception as e:
            logger.warning(f"Error validating {tile.id}: {e}, attempting re-encode")
            return self._reencode_tile(tile, local_path)

    def _reencode_tile(self, tile: Tile, original_path: Path) -> Optional[Tile]:
        """Re-encode tile to standard DEFLATE compression.

        Parameters
        ----------
        tile : Tile
            Tile to re-encode.
        original_path : Path
            Original tile file path.

        Returns
        -------
        Tile | None
            The tile if re-encoding succeeded, None otherwise.
        """
        try:
            temp_path = original_path.with_suffix(".reenc.tif")

            # Use gdal_translate to re-encode with DEFLATE compression
            result = subprocess.run(
                [
                    "gdal_translate",
                    "-co", "COMPRESS=DEFLATE",
                    "-co", "PREDICTOR=3",
                    "-co", "TILED=YES",
                    "-co", "BLOCKXSIZE=512",
                    "-co", "BLOCKYSIZE=512",
                    str(original_path),
                    str(temp_path),
                ],
                capture_output=True,
                timeout=60,
                text=True,
            )

            if result.returncode != 0:
                logger.error(
                    f"gdal_translate failed for {tile.id}: {result.stderr[:300]}"
                )
                return None

            # Verify re-encoded tile is readable
            verify_result = subprocess.run(
                ["gdalinfo", "-checksum", str(temp_path)],
                capture_output=True,
                timeout=10,
                text=True,
            )

            if verify_result.returncode != 0:
                logger.error(f"Re-encoded tile {tile.id} is still unreadable")
                temp_path.unlink(missing_ok=True)
                return None

            # Replace original with re-encoded version
            shutil.move(str(temp_path), str(original_path))
            logger.info(f"✓ Successfully re-encoded {tile.id} to DEFLATE")
            return tile

        except subprocess.TimeoutExpired:
            logger.error(f"Re-encoding timeout for {tile.id}")
            return None
        except Exception as e:
            logger.error(f"Failed to re-encode {tile.id}: {e}")
            return None
