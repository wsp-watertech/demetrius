"""Dataset prioritization and duplicate resolution."""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Sequence

from .models import Tile

logger = logging.getLogger(__name__)


def prioritize_datasets(tiles: Sequence[Tile]) -> list[Tile]:
    """Assign priority values to tiles and datasets.

    Datasets are prioritized by:
    1. publication_date (ascending - oldest first for base layer)
    2. last_updated (fallback)

    Matching tiles from different datasets are NOT deduplicated here.
    Instead, all tiles are kept and allowed to overlap. The mosaic step
    will resolve overlaps by selecting the most recent non-nodata value.

    Args:
        tiles: Raw tiles from TNM

    Returns:
        All tiles with priority assigned (no deduplication)
    """
    logger.info(f"Prioritizing datasets among {len(tiles)} tiles")

    # Group tiles by dataset_id
    datasets: dict[str, list[Tile]] = defaultdict(list)
    for tile in tiles:
        datasets[tile.dataset_id].append(tile)

    logger.info(f"Found {len(datasets)} unique datasets")

    # Sort datasets by priority
    sorted_datasets = _sort_datasets_by_priority(list(datasets.keys()), tiles)

    logger.info(f"Dataset priority order: {sorted_datasets}")

    # Assign priority values to all tiles (no deduplication)
    result = []

    for priority, dataset_id in enumerate(sorted_datasets):
        for tile in datasets[dataset_id]:
            tile.priority = priority
            result.append(tile)
            logger.debug(f"Assigned priority {priority} to tile {tile.tile_id} (dataset {dataset_id})")

    logger.info(f"Total tiles: {len(result)} (including overlapping tiles from different datasets)")
    return result


def _sort_datasets_by_priority(
    dataset_ids: list[str], all_tiles: Sequence[Tile]
) -> list[str]:
    """Sort dataset IDs by recency (newest first).

    Args:
        dataset_ids: List of unique dataset identifiers
        all_tiles: All tiles to search for metadata

    Returns:
        Sorted list of dataset IDs (newest first)
    """
    dataset_metadata: dict[str, tuple[datetime, datetime]] = {}

    for dataset_id in dataset_ids:
        dataset_tiles = [t for t in all_tiles if t.dataset_id == dataset_id]
        if not dataset_tiles:
            continue

        # Get newest publication date and last_updated in this dataset
        pub_dates = [t.publication_date for t in dataset_tiles]
        update_dates = [t.last_updated for t in dataset_tiles]

        latest_pub = max(pub_dates)
        latest_update = max(update_dates)

        dataset_metadata[dataset_id] = (latest_pub, latest_update)

    # Sort by publication_date asc, then last_updated asc (oldest first)
    # This ensures oldest data becomes priority=0 (base) and newest gets highest priority (overlay on top)
    sorted_ids = sorted(
        dataset_metadata.keys(),
        key=lambda d: (dataset_metadata[d][0], dataset_metadata[d][1]),
    )

    return sorted_ids
