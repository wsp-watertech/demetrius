"""Keep lidar acquisition dates separate from product publication dates."""

from datetime import datetime

import httpx
import pytest
from shapely.geometry import box

from src.demetrius.inspector import InspectionReport
from src.demetrius.manifest import Manifest
from src.demetrius.models import AOI, BoundingBox, Tile
from src.demetrius.priority import prioritize_datasets
from src.demetrius.tnm import TNMTileSource


def _tile(dataset, publication, start=None, end=None):
    return Tile(
        id=f"{dataset}_x0y0",
        dataset_id=dataset,
        tile_id="x0y0",
        publication_date=datetime.fromisoformat(publication),
        last_updated=datetime.fromisoformat(publication),
        flight_start=datetime.fromisoformat(start) if start else None,
        flight_end=datetime.fromisoformat(end) if end else None,
        download_url="https://example.com/tile.tif",
        bounds_wgs84=BoundingBox(min_x=-75, min_y=40, max_x=-74, max_y=41),
        priority=-1,
    )


def test_tnm_keeps_publication_and_both_sciencebase_flight_dates():
    source = TNMTileSource()
    calls = []

    def metadata(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "dates": [
                    {"type": "Publication", "dateString": "2025-04-30"},
                    {"type": "Start", "dateString": "2023-03-18"},
                    {"type": "End", "dateString": "2023-05-11"},
                ]
            },
        )

    source.client.close()
    source.client = httpx.Client(transport=httpx.MockTransport(metadata))
    item = {
        "title": "USGS 1 Meter 18 x41y430 DE_Statewide_B23",
        "downloadURL": "https://example.com/USGS_1M_18_x41y430_DE_Statewide_B23.tif",
        "publicationDate": "2025-04-30",
        "lastUpdated": "2025-05-08T02:17:07Z",
        "metaUrl": "https://www.sciencebase.gov/catalog/item/example",
        "boundingBox": {"minX": -75, "minY": 40, "maxX": -74, "maxY": 41},
    }
    first = source._parse_tnm_item(item)
    second = source._parse_tnm_item(item)

    assert first.publication_date.date().isoformat() == "2025-04-30"
    assert first.flight_start.date().isoformat() == "2023-03-18"
    assert first.flight_end.date().isoformat() == "2023-05-11"
    assert second.flight_end == first.flight_end
    assert len(calls) == 1
    source.client.close()


def test_missing_sciencebase_dates_do_not_become_publication_dates():
    source = TNMTileSource()
    item = {
        "title": "USGS 1 Meter 18 x41y430 DE_Statewide_B23",
        "downloadURL": "https://example.com/tile.tif",
        "publicationDate": "2025-04-30",
        "lastUpdated": "2025-05-08",
        "boundingBox": {"minX": -75, "minY": 40, "maxX": -74, "maxY": 41},
    }
    tile = source._parse_tnm_item(item)
    assert tile.flight_start is None and tile.flight_end is None
    assert tile.publication_date.date().isoformat() == "2025-04-30"
    source.client.close()


def test_sciencebase_failure_keeps_publication_and_reports_missing_flight(caplog):
    source = TNMTileSource()
    source.client.close()
    source.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with caplog.at_level("WARNING"):
        assert source._fetch_sciencebase_flight_dates("https://example.com/metadata") == (
            None,
            None,
        )
    assert "Could not fetch ScienceBase lidar flight dates" in caplog.text
    source.client.close()


def test_sciencebase_rejects_invalid_flight_range():
    source = TNMTileSource()
    source.client.close()
    source.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "dates": [
                        {"type": "Start", "dateString": "2024-06-01"},
                        {"type": "End", "dateString": "2024-05-01"},
                    ]
                },
            )
        )
    )
    with pytest.raises(ValueError, match="end precedes start"):
        source._fetch_sciencebase_flight_dates("https://example.com/metadata")
    source.client.close()


def test_priority_uses_flight_end_then_start_not_publication():
    tiles = [
        _tile("old_flight_new_publication", "2025-01-01", "2019-01-01", "2019-05-01"),
        _tile("new_flight_old_publication", "2022-01-01", "2020-01-01", "2020-05-01"),
        _tile("missing_flight", "2026-01-01"),
        _tile("same_end_later_start", "2021-01-01", "2020-02-01", "2020-05-01"),
    ]
    prioritized = prioritize_datasets(tiles)
    assert [t.dataset_id for t in prioritized] == [
        "missing_flight",
        "old_flight_new_publication",
        "new_flight_old_publication",
        "same_end_later_start",
    ]
    assert [t.priority for t in prioritized] == [0, 1, 2, 3]


def test_priority_never_uses_publication_as_flight_tie_breaker():
    tiles = [
        _tile("a", "2025-01-01", "2020-01-01", "2020-05-01"),
        _tile("b", "2021-01-01", "2020-01-01", "2020-05-01"),
    ]
    assert [t.dataset_id for t in prioritize_datasets(tiles)] == ["a", "b"]


def test_inspection_reports_both_dates_in_summary_and_details():
    aoi = AOI(geometry=box(-75, 40, -74, 41))
    tiles = prioritize_datasets(
        [
            _tile("project", "2025-04-30", "2023-03-18", "2023-05-11"),
            _tile("project", "2025-05-01", "2023-04-18", "2023-06-11"),
        ]
    )
    report = InspectionReport(tiles, aoi)
    for output in (report.summary(), report.verbose_details()):
        assert "LiDAR flight dates: 2023-03-18 → 2023-06-11" in output
        assert "Publication dates: 2025-04-30 → 2025-05-01" in output


def test_manifest_round_trip_preserves_temporal_metadata(tmp_path):
    tile = _tile("project", "2025-04-30", "2023-03-18", "2023-05-11")
    manifest = Manifest(AOI(geometry=box(-75, 40, -74, 41)), [tile])
    path = tmp_path / "dem.manifest.json"
    manifest.save(path)
    restored = Manifest.load(path).tiles[0]
    assert restored.publication_date == tile.publication_date
    assert restored.last_updated == tile.last_updated
    assert restored.flight_start == tile.flight_start
    assert restored.flight_end == tile.flight_end


def test_report_marks_missing_flight_dates_unknown():
    report = InspectionReport([_tile("unknown", "2025-04-30")], AOI(geometry=box(-75, 40, -74, 41)))
    assert "LiDAR flight dates: unknown → unknown" in report.summary()
    assert "Publication dates: 2025-04-30" in report.summary()
