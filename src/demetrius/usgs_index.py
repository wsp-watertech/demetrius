"""USGS Elevation Index queries with resilient footprint retrieval."""

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)
REQUEST_ATTEMPTS = 3


class _RetryableQueryError(ValueError):
    """A USGS server or transport error that persisted after retries."""


class FootprintQuery:
    """Fetch complete paginated project footprints from an ArcGIS index layer."""

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        label: str,
        required_fields: tuple[str, ...],
        page_size: int,
    ):
        self.client = client
        self.url = url
        self.label = label
        self.required_fields = required_fields
        self.page_size = page_size

    def _request_json(self, params: dict[str, str | int]) -> dict[str, Any]:
        for attempt in range(REQUEST_ATTEMPTS):
            try:
                response = self.client.get(self.url, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError("response is not a JSON object")
                error = payload.get("error")
                if error:
                    message = f"{self.label} query failed: {error}"
                    if (
                        isinstance(error, dict)
                        and isinstance(error.get("code"), int)
                        and error["code"] >= 500
                    ):
                        raise _RetryableQueryError(message)
                    raise ValueError(message)
                return payload
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise ValueError(f"{self.label} query failed: {exc}") from exc
                failure = _RetryableQueryError(f"{self.label} query failed: {exc}")
            except httpx.RequestError as exc:
                failure = _RetryableQueryError(f"{self.label} query failed: {exc}")
            except _RetryableQueryError as exc:
                failure = exc
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid {self.label} response: {exc}") from exc
            if attempt + 1 == REQUEST_ATTEMPTS:
                raise failure
            logger.warning("%s query failed, retrying: %s", self.label, failure)
            time.sleep(2**attempt)
        raise AssertionError("Unreachable")

    def _pages(self, params: dict[str, str | int], *, geometry: bool) -> list[dict[str, Any]]:
        features: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        while True:
            payload = self._request_json({**params, "resultOffset": len(features)})
            page = payload.get("features")
            if not isinstance(page, list) or (
                geometry and payload.get("type") != "FeatureCollection"
            ):
                raise ValueError(f"{self.label} query returned invalid GeoJSON or attributes")
            if any(not isinstance(feature, dict) for feature in page):
                raise ValueError(f"{self.label} query returned invalid GeoJSON or attributes")
            for feature in page:
                props = feature.get("properties" if geometry else "attributes")
                if (
                    not isinstance(props, dict)
                    or any(props.get(key) is None for key in self.required_fields)
                    or (geometry and not feature.get("geometry"))
                    or (not geometry and type(props.get("OBJECTID")) is not int)
                ):
                    raise ValueError(f"{self.label} query returned invalid GeoJSON or attributes")
                if not geometry:
                    object_id = props["OBJECTID"]
                    if object_id in seen_ids:
                        raise ValueError(f"Duplicate {self.label} OBJECTID {object_id}")
                    seen_ids.add(object_id)
            features.extend(page)
            if not page and payload.get("exceededTransferLimit"):
                raise ValueError(f"{self.label} query returned an empty truncated page")
            if not payload.get("exceededTransferLimit") and len(page) < self.page_size:
                return features

    def _individual_footprints(self, params: dict[str, str | int]) -> list[dict[str, Any]]:
        attributes_params = {
            **params,
            "outFields": f"OBJECTID,{params['outFields']}",
            "returnGeometry": "false",
            "f": "json",
        }
        records = self._pages(attributes_params, geometry=False)
        features = []
        for record in records:
            props = record["attributes"]
            object_id = props["OBJECTID"]
            geometry_params = {
                **params,
                "outFields": "OBJECTID",
                "objectIds": str(object_id),
                "resultRecordCount": 1,
            }
            try:
                try:
                    payload = self._request_json(geometry_params)
                except _RetryableQueryError:
                    logger.warning(
                        "%s OBJECTID %s failed; retrying with coordinates "
                        "rounded to nine decimal places",
                        self.label,
                        object_id,
                    )
                    payload = self._request_json({**geometry_params, "geometryPrecision": 9})
            except ValueError as exc:
                raise ValueError(
                    f"Could not retrieve {self.label} footprint OBJECTID {object_id}: {exc}"
                ) from exc
            page = payload.get("features")
            if (
                payload.get("type") != "FeatureCollection"
                or payload.get("exceededTransferLimit")
                or not isinstance(page, list)
                or len(page) != 1
                or not isinstance(page[0], dict)
                or not page[0].get("geometry")
                or not isinstance(page[0].get("properties"), dict)
                or page[0]["properties"].get("OBJECTID") != object_id
            ):
                raise ValueError(
                    f"Invalid or missing {self.label} footprint for OBJECTID {object_id}"
                )
            features.append(
                {"type": "Feature", "geometry": page[0]["geometry"], "properties": props}
            )
        return features

    def fetch(self, params: dict[str, str | int]) -> list[dict[str, Any]]:
        """Fetch all footprints, falling back only on persistent server/transport failures."""
        try:
            return self._pages(params, geometry=True)
        except _RetryableQueryError as exc:
            logger.warning(
                "Combined %s query failed; fetching individual footprints: %s", self.label, exc
            )
            return self._individual_footprints(params)
