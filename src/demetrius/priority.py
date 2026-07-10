"""Dataset prioritization and duplicate resolution."""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Sequence

from .models import Tile

logger = logging.getLogger(__name__)


def prioritize_datasets(tiles: Sequence[Tile]) -> list[Tile]:
    """Assign priority values and resolve duplicate tiles.

    Datasets are prioritized by:
    1. publication_date (descending - newest first)
    2. last_updated (fallback)

    When the same tile appears in multiple datasets, only the
    highest-priority (newest) version is kept.

    Args:
        tiles: Raw tiles from TNM

    Returns:
        Tiles with priority assigned, duplicates removed
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

    # Assign priority values and track seen tiles
    result = []
    seen_tile_ids: set[str] = set()

    for priority, dataset_id in enumerate(sorted_datasets):
        for tile in datasets[dataset_id]:
            if tile.tile_id not in seen_tile_ids:
                tile.priority = priority
                result.append(tile)
                seen_tile_ids.add(tile.tile_id)
                logger.debug(f"Assigned priority {priority} to tile {tile.tile_id} (dataset {dataset_id})")
            else:
                logger.debug(
                    f"Skipped duplicate tile {tile.tile_id} from dataset {dataset_id} "
                    f"(already seen in higher-priority dataset)"
                )

    logger.info(f"After deduplication: {len(result)} tiles retained")
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

    # Sort by publication_date desc, then last_updated desc
    sorted_ids = sorted(
        dataset_metadata.keys(),
        key=lambda d: (dataset_metadata[d][0], dataset_metadata[d][1]),
        reverse=True,
    )

    return sorted_ids
