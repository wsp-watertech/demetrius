"""TNM Access API client for querying USGS 3DEP tiles."""

import logging
import time
from datetime import datetime
from typing import Any, Optional

import httpx

from .models import BoundingBox, Tile
from .parser import parse_dataset_and_tile_ids
from .sources import TileSource

logger = logging.getLogger(__name__)

TNM_API_BASE = "https://tnmaccess.nationalmap.gov/api/v1/products"
SCIENCEBASE_API_BASE = "https://www.sciencebase.gov/catalog/item"
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0  # seconds
MAX_BACKOFF = 60.0  # seconds
DEFAULT_PAGE_SIZE = 25  # Reduced from 50 for more resilient requests


class TNMTileSource(TileSource):
    """Query USGS TNM Access API for 3DEP DEM tiles."""

    def __init__(self, timeout: float = 30.0):
        """Initialize TNM client.

        Parameters
        ----------
        timeout : float, default=30.0
            Request timeout in seconds.

        Returns
        -------
        None
            Initializes the TNM tile source.
        """
        self.timeout = timeout
        self.client = httpx.Client(timeout=timeout)
        self._sciencebase_cache: dict[str, tuple[Optional[datetime], Optional[datetime]]] = {}

    def search(self, aoi_bbox: BoundingBox) -> list[Tile]:
        """Search TNM for tiles intersecting bounding box.

        Paginates through all results from TNM Access API (backed by ScienceBase).
        Retries with exponential backoff on 5xx errors (server errors).

        Parameters
        ----------
        aoi_bbox : BoundingBox
            Bounding box in EPSG:4326.

        Returns
        -------
        list[Tile]
            Tile objects returned from TNM.

        Raises
        ------
        ValueError
            If the TNM query fails after max retries or returns invalid JSON.
        """
        logger.info(
            f"Querying TNM for tiles in bbox: {aoi_bbox.min_x}, {aoi_bbox.min_y}, "
            f"{aoi_bbox.max_x}, {aoi_bbox.max_y}"
        )

        tiles = []
        offset = 0
        page_size = DEFAULT_PAGE_SIZE

        total = None  # Will be determined from first response with valid total

        while True:
            params = {
                "bbox": f"{aoi_bbox.min_x},{aoi_bbox.min_y},{aoi_bbox.max_x},{aoi_bbox.max_y}",
                "datasets": "Digital Elevation Model (DEM) 1 meter",
                "prodFormats": "GeoTIFF",
                "outputFormat": "JSON",
                "max": page_size,
                "offset": offset,
            }

            # Retry loop for this page
            response = self._get_with_backoff(params)

            try:
                data = response.json()
            except ValueError as e:
                raise ValueError(f"TNM API returned invalid JSON: {e}") from e

            response_total = data.get("total", 0)
            items = data.get("items", [])

            # Use first valid (non-zero) total from any response
            if total is None and response_total > 0:
                total = response_total
                logger.debug(f"Total items from TNM API: {total}")
            elif total is None and response_total == 0:
                logger.debug(
                    f"TNM API returned total=0 (possibly transient API state); "
                    f"continuing pagination until empty response"
                )

            logger.debug(f"Fetched {len(items)} items (offset={offset}, total={total})")

            # Parse items on this page
            for item in items:
                try:
                    tile = self._parse_tnm_item(item)
                    tiles.append(tile)
                    logger.debug(f"Parsed tile: {tile.id}")
                except ValueError as e:
                    logger.warning(f"Skipped TNM item: {e}")
                    continue

            # Stop pagination when we get zero items (empty page)
            # OR when we've reached the declared total (if total is known and valid)
            if not items:
                logger.debug("Received empty page, stopping pagination")
                break

            if total is not None and total > 0 and offset + len(items) >= total:
                logger.debug(f"Reached declared total ({total}), stopping pagination")
                break

            offset += page_size

        if total is None or total == 0:
            total = len(tiles)  # Fallback to actual count if API never provided valid total

        logger.info(f"Found {len(tiles)} tiles from TNM (total {total} items)")
        return tiles

    def _get_with_backoff(self, params: dict) -> httpx.Response:
        """Execute TNM API request with exponential backoff retry on 5xx errors.

        Parameters
        ----------
        params : dict
            Query parameters for TNM API request.

        Returns
        -------
        httpx.Response
            Successful response from TNM API.

        Raises
        ------
        ValueError
            If the request fails after max retries or on 4xx client errors.
        """
        backoff = INITIAL_BACKOFF
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.get(TNM_API_BASE, params=params)

                # 5xx errors: retry with backoff
                if 500 <= response.status_code < 600:
                    last_error = f"HTTP {response.status_code}"
                    if attempt < MAX_RETRIES - 1:
                        logger.warning(
                            f"TNM API returned {response.status_code}, retrying in {backoff:.1f}s "
                            f"(attempt {attempt + 1}/{MAX_RETRIES})"
                        )
                        time.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        continue
                    else:
                        raise ValueError(
                            f"TNM API request failed after {MAX_RETRIES} retries: {last_error}"
                        )

                # 4xx errors: fail immediately (client error, not server issue)
                response.raise_for_status()
                return response

            except httpx.TimeoutException as e:
                last_error = f"Timeout: {e}"
                if attempt < MAX_RETRIES - 1:
                    logger.warning(
                        f"TNM API request timed out, retrying in {backoff:.1f}s "
                        f"(attempt {attempt + 1}/{MAX_RETRIES})"
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    continue
                else:
                    raise ValueError(
                        f"TNM API request failed after {MAX_RETRIES} retries: {last_error}"
                    ) from e

            except httpx.HTTPError as e:
                raise ValueError(f"TNM API request failed: {e}") from e

        raise ValueError(f"TNM API request failed after {MAX_RETRIES} retries: {last_error}")

    def _parse_tnm_item(self, item: dict[str, Any]) -> Tile:
        """Parse a single TNM product item into Tile object.

        Parameters
        ----------
        item : dict[str, Any]
            TNM API response item.

        Returns
        -------
        Tile
            Parsed tile object.

        Raises
        ------
        ValueError
            If required fields are missing or invalid.
        """
        required_fields = ["title", "downloadURL", "publicationDate", "lastUpdated", "boundingBox"]
        for field in required_fields:
            if field not in item:
                raise ValueError(f"Missing required field: {field}")

        title = item["title"]
        url = item["downloadURL"]
        bbox = item["boundingBox"]

        # Parse dataset and tile IDs from title
        try:
            dataset_id, tile_id = parse_dataset_and_tile_ids(title, url)
        except ValueError as e:
            raise ValueError(f"Failed to parse IDs from '{title}': {e}") from e

        # TNM publication and ScienceBase acquisition dates describe different events.
        try:
            pub_date = self._parse_date(item["publicationDate"])
            last_updated = self._parse_date(item["lastUpdated"])
        except ValueError as e:
            raise ValueError(f"Invalid date format: {e}") from e
        flight_start, flight_end = self._fetch_sciencebase_flight_dates(item.get("metaUrl"))

        # Validate bounding box
        try:
            bounds = BoundingBox(
                min_x=float(bbox["minX"]),
                min_y=float(bbox["minY"]),
                max_x=float(bbox["maxX"]),
                max_y=float(bbox["maxY"]),
            )
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"Invalid boundingBox: {e}") from e

        # Create tile ID
        tile_full_id = f"{dataset_id}_{tile_id}"

        return Tile(
            id=tile_full_id,
            dataset_id=dataset_id,
            tile_id=tile_id,
            publication_date=pub_date,
            last_updated=last_updated,
            flight_start=flight_start,
            flight_end=flight_end,
            download_url=url,
            bounds_wgs84=bounds,
            priority=-1,  # Will be assigned during prioritization
        )

    def _parse_date(self, date_str: str) -> datetime:
        """Parse date string from TNM response.

        TNM uses formats like:
        - "2021-11-18" (date only)
        - "2021-11-22T17:32:57" (ISO with time)
        - "2021-11-22T17:32:57.123" (ISO with milliseconds)
        - "2022-05-24T23:02:12.343-06:00" (ISO8601 with timezone)

        Parameters
        ----------
        date_str : str
            Date string from TNM.

        Returns
        -------
        datetime
            Parsed datetime object with timezone information removed.

        Raises
        ------
        ValueError
            If the date string cannot be parsed.
        """
        try:
            parsed = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Could not parse date: {date_str}") from exc
        return parsed.replace(tzinfo=None)

    def _fetch_sciencebase_flight_dates(
        self, metaUrl: Optional[str]
    ) -> tuple[Optional[datetime], Optional[datetime]]:
        """Fetch lidar acquisition start and end dates from ScienceBase metadata.

        Parameters
        ----------
        metaUrl : str | None
            Sciencebase metadata URL from TNM item.

        Returns
        -------
        tuple[datetime | None, datetime | None]
            Flight start and end, if present.
        """
        if not metaUrl:
            return None, None

        # Check cache first
        if metaUrl in self._sciencebase_cache:
            return self._sciencebase_cache[metaUrl]

        try:
            # Fetch sciencebase metadata
            sb_url = f"{metaUrl}?format=json"
            response = self.client.get(sb_url)
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise ValueError(f"Invalid ScienceBase metadata for {metaUrl}")
            dates = data.get("dates", [])
            if not isinstance(dates, list):
                raise ValueError(f"Invalid ScienceBase dates for {metaUrl}")
            parsed_dates: dict[str, datetime] = {}
            for entry in dates:
                if isinstance(entry, dict) and entry.get("type") in ("Start", "End"):
                    if not isinstance(entry.get("dateString"), str):
                        raise ValueError(f"Invalid ScienceBase flight date for {metaUrl}")
                    parsed_dates[entry["type"]] = self._parse_date(entry["dateString"])
            flight_start = parsed_dates.get("Start")
            flight_end = parsed_dates.get("End")
            if flight_start is not None and flight_end is not None and flight_start > flight_end:
                raise ValueError(f"ScienceBase flight end precedes start for {metaUrl}")
            if flight_start is None or flight_end is None:
                logger.warning("Incomplete ScienceBase lidar flight dates for %s", metaUrl)
            flight_dates = flight_start, flight_end
            self._sciencebase_cache[metaUrl] = flight_dates
            return flight_dates
        except httpx.HTTPError as exc:
            logger.warning(
                "Could not fetch ScienceBase lidar flight dates from %s: %s", metaUrl, exc
            )
            self._sciencebase_cache[metaUrl] = (None, None)
            return None, None

    def __del__(self) -> None:
        """Clean up the HTTP client.

        Returns
        -------
        None
            Closes the HTTP client when the instance is destroyed.
        """
        if hasattr(self, "client"):
            self.client.close()
