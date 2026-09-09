from datetime import datetime, timezone

import pytest

from enmap_brazil.stac import Collection, StacClient, StacError, parse_datetime
from tests.fake_stac import COLLECTION_ID, FakeStacServer


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2024-05-06T12:00:00Z", datetime(2024, 5, 6, 12, tzinfo=timezone.utc)),
        ("2024-05-06T12:00:00+00:00", datetime(2024, 5, 6, 12, tzinfo=timezone.utc)),
        ("2024-05-06", datetime(2024, 5, 6, tzinfo=timezone.utc)),
        # Nine fractional digits: valid RFC 3339, rejected by fromisoformat.
        ("2024-05-06T12:00:00.123456789Z", datetime(2024, 5, 6, 12, 0, 0, 123456, timezone.utc)),
        (None, None),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_datetime(raw, expected):
    assert parse_datetime(raw) == expected


def test_parse_datetime_assumes_utc_when_naive():
    assert parse_datetime("2024-05-06T12:00:00").tzinfo == timezone.utc


def test_collection_from_json_reads_the_temporal_extent():
    coll = Collection.from_json(
        {
            "id": "X",
            "extent": {
                "spatial": {"bbox": [[-10, -10, 10, 10]]},
                "temporal": {"interval": [["2022-11-02T00:00:00Z", None]]},
            },
        }
    )
    assert coll.temporal_start == datetime(2022, 11, 2, tzinfo=timezone.utc)
    assert coll.temporal_end is None  # an open-ended, still-flying mission
    assert coll.bbox == [-10, -10, 10, 10]


def test_enmap_collections_filters_out_unrelated_missions(stac_server):
    with StacClient(stac_server.url, rate_limit=0) as client:
        assert [c.id for c in client.collections()] == [COLLECTION_ID, "TDM90_DEM"]
        assert [c.id for c in client.enmap_collections()] == [COLLECTION_ID]


def test_pagination_walks_every_page(stac_server):
    with StacClient(stac_server.url, rate_limit=0) as client:
        pages = list(client.iter_search(collections=[COLLECTION_ID], limit=10))
    assert len(pages) == 3
    ids = [f["id"] for page in pages for f in page.features]
    assert len(ids) == stac_server.item_count == len(set(ids))
    assert pages[0].matched == stac_server.item_count


def test_falls_back_to_get_when_post_search_is_unavailable():
    server = FakeStacServer(allow_post=False)
    url = server.start()
    try:
        with StacClient(url, rate_limit=0) as client:
            pages = list(client.iter_search(collections=[COLLECTION_ID], limit=10))
        assert sum(len(p.features) for p in pages) == server.item_count
        assert ("POST", "/search") in server.requests, "should have tried POST first"
        assert any(m == "GET" and p.startswith("/search") for m, p in server.requests)
    finally:
        server.stop()


def test_retries_transient_server_errors(monkeypatch):
    monkeypatch.setattr("enmap_brazil.stac.time.sleep", lambda _s: None)
    server = FakeStacServer(fail_times=2)
    url = server.start()
    try:
        with StacClient(url, rate_limit=0, max_retries=5) as client:
            page = client.search_page(collections=[COLLECTION_ID], limit=10)
        assert len(page.features) == 10
    finally:
        server.stop()


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr("enmap_brazil.stac.time.sleep", lambda _s: None)
    server = FakeStacServer(fail_times=99)
    url = server.start()
    try:
        with StacClient(url, rate_limit=0, max_retries=1) as client:
            with pytest.raises(StacError):
                client.search_page(collections=[COLLECTION_ID], limit=10)
    finally:
        server.stop()


def test_datetime_filter_is_passed_through(stac_server):
    with StacClient(stac_server.url, rate_limit=0) as client:
        page = client.search_page(
            collections=[COLLECTION_ID],
            datetime_range="2022-11-01T00:00:00Z/2022-12-31T23:59:59Z",
            limit=100,
        )
    for feature in page.features:
        assert feature["properties"]["datetime"] < "2023-01"


def test_repeated_next_link_does_not_loop_forever(stac_server, monkeypatch):
    """A server that keeps handing back the same cursor must not hang the harvest."""
    with StacClient(stac_server.url, rate_limit=0) as client:
        first = client.search_page(collections=[COLLECTION_ID], limit=10)
        monkeypatch.setattr(client, "_page_from", lambda doc, mode: first)
        pages = list(client.iter_search(collections=[COLLECTION_ID], limit=10))
    assert len(pages) <= 3
