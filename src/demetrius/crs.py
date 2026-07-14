"""Multi-CRS handling and detection."""

import logging
import re
from typing import Optional, Sequence

from .models import Tile

logger = logging.getLogger(__name__)


def extract_utm_zone(crs: Optional[str]) -> Optional[int]:
    """Extract UTM zone number from EPSG code.

    Args:
        crs: CRS string like "EPSG:32618"

    Returns:
        Zone number (1-60) or None if not UTM
    """
    if not crs:
        return None

    match = re.search(r"32[67](\d{2})", crs)
    if match:
        return int(match.group(1))

    return None


def detect_utm_zones_from_tiles(tiles: Sequence[Tile]) -> set[int]:
    """Detect which UTM zones tiles span based on longitude.

    This is a heuristic approach - in production, would use actual tile metadata.

    Args:
        tiles: Tiles to analyze

    Returns:
        Set of UTM zone numbers (1-60)
    """
    zones: set[int] = set()

    for tile in tiles:
        # UTM zones are 6 degrees wide, starting at -180
        # Zone 1: -180 to -174
        # Zone 31: -6 to 0
        # Zone 32: 0 to 6
        # Zone 60: 174 to 180
        center_lon = (tile.bounds_wgs84.min_x + tile.bounds_wgs84.max_x) / 2
        zone = int((center_lon + 180) / 6) + 1
        zone = max(1, min(60, zone))  # Clamp to 1-60
        zones.add(zone)

    return zones


def get_target_utm_for_tiles(tiles: Sequence[Tile]) -> str:
    """Determine target UTM zone for tiles.

    If tiles span multiple UTM zones, selects the zone covering the most tiles.

    Args:
        tiles: Tiles to analyze

    Returns:
        EPSG code for target UTM zone (e.g., "EPSG:32618")
    """
    zones = detect_utm_zones_from_tiles(tiles)

    if not zones:
        raise ValueError("Could not determine UTM zone from tiles")

    # Find zone with most tiles
    zone_counts = {}
    for tile in tiles:
        center_lon = (tile.bounds_wgs84.min_x + tile.bounds_wgs84.max_x) / 2
        zone = int((center_lon + 180) / 6) + 1
        zone = max(1, min(60, zone))
        zone_counts[zone] = zone_counts.get(zone, 0) + 1

    selected_zone = max(zone_counts, key=zone_counts.get)

    # Convert zone to EPSG code
    # Northern hemisphere: 32600 + zone
    # Southern hemisphere: 32700 + zone
    # For now, assume Northern hemisphere (can be enhanced with tile bounds)
    epsg_code = 32600 + selected_zone

    logger.info(
        f"Selected UTM zone {selected_zone} (EPSG:{epsg_code}) "
        f"covering {zone_counts[selected_zone]}/{len(tiles)} tiles"
    )

    return f"EPSG:{epsg_code}"
