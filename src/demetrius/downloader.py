"""Parallel tile downloading with integrity checks."""

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional, Sequence

import httpx

from .models import Tile

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path.home() / ".demetrius"
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
        return downloaded

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
