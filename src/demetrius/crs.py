"""Multi-CRS handling and detection."""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from .models import Tile

logger = logging.getLogger(__name__)


def get_crs_from_raster(raster_path: str) -> Optional[str]:
    """Extract CRS from a raster file using gdalinfo.

    Parameters
    ----------
    raster_path : str
        Path to the raster file.

    Returns
    -------
    str | None
        CRS string such as ``"EPSG:26917"`` or None if detection fails.
    """
    try:
        result = subprocess.run(
            ["gdalinfo", "-json", str(raster_path)],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
            timeout=10,
        )

        if result.returncode != 0:
            logger.debug(f"gdalinfo failed for {raster_path}: {result.stderr}")
            return None

        # Parse JSON output
        info = json.loads(result.stdout)

        # Look for EPSG code in coordinateSystem
        if "coordinateSystem" in info:
            coord_sys = info["coordinateSystem"]

            if "wkt" in coord_sys:
                wkt = coord_sys["wkt"]

                # Try modern WKT format: ID["EPSG",26917]
                # Get the last occurrence (the main CRS ID, not sub-component IDs)
                matches = list(re.finditer(r'ID\["EPSG",(\d{5})\]', wkt))
                if matches:
                    return f"EPSG:{matches[-1].group(1)}"

                # Try old WKT format: AUTHORITY["EPSG","32618"]
                match = re.search(r'AUTHORITY\["EPSG","(\d{5})"\]', wkt)
                if match:
                    return f"EPSG:{match.group(1)}"

        return None

    except json.JSONDecodeError:
        logger.debug(f"Failed to parse gdalinfo JSON for {raster_path}")
        return None
    except subprocess.TimeoutExpired:
        logger.debug(f"gdalinfo timed out for {raster_path}")
        return None
    except FileNotFoundError:
        logger.debug("gdalinfo not found")
        return None
    except Exception as e:
        logger.debug(f"Failed to detect CRS from {raster_path}: {e}")
        return None


def extract_utm_zone(crs: Optional[str]) -> Optional[int]:
    """Extract UTM zone number from EPSG code.

    Parameters
    ----------
    crs : str | None
        CRS string such as ``"EPSG:32618"``.

    Returns
    -------
    int | None
        Zone number from 1 to 60, or ``None`` if the CRS is not UTM.
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

    Parameters
    ----------
    tiles : Sequence[Tile]
        Tiles to analyze.

    Returns
    -------
    set[int]
        UTM zone numbers from 1 to 60 represented by the tiles.
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

    Parameters
    ----------
    tiles : Sequence[Tile]
        Tiles to analyze.

    Returns
    -------
    str
        EPSG code for the target UTM zone, such as ``"EPSG:32618"``.

    Raises
    ------
    ValueError
        If no UTM zone can be determined from the tiles.
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
