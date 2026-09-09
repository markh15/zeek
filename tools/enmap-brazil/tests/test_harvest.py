import threading
from datetime import datetime, timezone


from enmap_brazil.config import Settings
from enmap_brazil.db import Database
from enmap_brazil.harvest import Harvester
from enmap_brazil.regions import BRAZIL_BBOX
from enmap_brazil.stac import StacClient, StacError
from tests.fake_stac import COLLECTION_END, COLLECTION_ID, COLLECTION_START

TEN_YEARS_AGO = datetime(2015, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2025, 9, 1, tzinfo=timezone.utc)


def make_settings(db_path, **kwargs):
    base = dict(
        db_path=db_path,
        bbox=BRAZIL_BBOX,
        start=TEN_YEARS_AGO,
        end=NOW,
        window_months=12,
        rate_limit=0.0,
        page_limit=10,
    )
    base.update(kwargs)
    return Settings(**base)


def run_harvest(server, db_path, **kwargs):
    settings = make_settings(db_path, stac_root=server.url, **kwargs)
    events = []
    with Database(db_path) as db, StacClient(server.url, rate_limit=0) as client:
        harvester = Harvester(settings, db, client, on_event=events.append)
        result = harvester.run()
    return result, events


def test_harvest_fills_the_database(stac_server, db_path):
    result, _ = run_harvest(stac_server, db_path)
    assert result.status == "ok"
    assert result.items_new == stac_server.item_count
    with Database(db_path) as db:
        assert db.scene_count() == stac_server.item_count
        assert db.stats()["per_collection"][0]["collection"] == COLLECTION_ID
        rows = db.query_scenes(bbox=BRAZIL_BBOX, limit=1000)
        assert len(rows) == stac_server.item_count


def test_ten_year_request_is_clamped_to_the_mission_lifetime(stac_server, db_path):
    """The whole point of 'ten years or however long it's available'."""
    result, events = run_harvest(stac_server, db_path, window_months=12)
    messages = " ".join(e.message for e in events)
    assert "clamped to" in messages
    with Database(db_path) as db:
        chunks = db.conn.execute(
            "SELECT MIN(dt_start) AS lo, MAX(dt_end) AS hi FROM harvest_chunks"
        ).fetchone()
    # Nothing before the launch date is ever requested, despite asking for 2015.
    assert chunks["lo"] >= COLLECTION_START.strftime("%Y-01-01T00:00:00Z")
    assert chunks["lo"] < "2023-01-01"
    assert chunks["hi"] <= COLLECTION_END.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert result.chunks_total < 10, "a 10-year sweep would be many more chunks"


def test_second_run_resumes_and_skips_finished_chunks(stac_server, db_path):
    first, _ = run_harvest(stac_server, db_path)
    assert first.chunks_skipped == 0
    before = len(stac_server.requests)

    searches_before = [r for r in stac_server.requests if "/search" in r[1] or "/items" in r[1]]
    second, _ = run_harvest(stac_server, db_path)
    assert second.chunks_skipped == first.chunks_total
    assert second.items_seen == 0
    searches_after = [r for r in stac_server.requests if "/search" in r[1] or "/items" in r[1]]
    assert searches_after == searches_before, "resume must not re-run any search"
    # Only the collection listing is re-fetched, and only once per run.
    assert len(stac_server.requests) - before == 1
    with Database(db_path) as db:
        assert db.scene_count() == stac_server.item_count


def test_no_resume_refetches_without_duplicating(stac_server, db_path):
    run_harvest(stac_server, db_path)
    result, _ = run_harvest(stac_server, db_path, resume=False)
    assert result.chunks_skipped == 0
    assert result.items_seen == stac_server.item_count
    assert result.items_new == 0, "already-known scenes are updated, not duplicated"
    with Database(db_path) as db:
        assert db.scene_count() == stac_server.item_count


def test_stop_event_halts_the_run_and_leaves_the_chunk_resumable(stac_server, db_path):
    settings = make_settings(db_path, stac_root=stac_server.url, window_months=1)
    stop = threading.Event()
    events = []

    def on_event(event):
        events.append(event)
        if event.kind == "chunk-done":
            stop.set()  # bail out after the first completed chunk

    with Database(db_path) as db, StacClient(stac_server.url, rate_limit=0) as client:
        result = Harvester(settings, db, client, on_event=on_event, stop_event=stop).run()
        assert result.status == "stopped"
        assert result.chunks_done < result.chunks_total
        statuses = {
            r["status"]
            for r in db.conn.execute("SELECT status FROM harvest_chunks").fetchall()
        }
    assert "running" not in statuses, "no chunk may be left wedged in 'running'"


def test_unknown_collection_is_rejected(stac_server, db_path):
    result, _ = run_harvest(stac_server, db_path, collections=["NOT_A_COLLECTION"])
    assert result.status == "error"
    assert "not found" in (result.error or "")


def test_explicit_collection_is_honoured(stac_server, db_path):
    result, _ = run_harvest(stac_server, db_path, collections=[COLLECTION_ID])
    assert result.collections == [COLLECTION_ID]
    assert result.items_new == stac_server.item_count


def test_range_entirely_before_launch_harvests_nothing(stac_server, db_path):
    result, events = run_harvest(
        stac_server,
        db_path,
        start=datetime(2012, 1, 1, tzinfo=timezone.utc),
        end=datetime(2014, 1, 1, tzinfo=timezone.utc),
    )
    assert result.status == "empty"
    assert result.chunks_total == 0
    assert "does not overlap" in " ".join(e.message for e in events)


def test_a_failing_chunk_does_not_abort_the_whole_run(stac_server, db_path, monkeypatch):
    settings = make_settings(db_path, stac_root=stac_server.url, window_months=1)
    calls = {"n": 0}

    with Database(db_path) as db, StacClient(stac_server.url, rate_limit=0) as client:
        real_iter = client.iter_search

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise StacError("boom")
            return real_iter(**kwargs)

        monkeypatch.setattr(client, "iter_search", flaky)
        result = Harvester(settings, db, client, on_event=lambda e: None).run()

    assert result.status == "ok"
    assert result.chunks_failed == 1
    assert result.chunks_done == result.chunks_total - 1
    with Database(db_path) as db:
        errored = db.conn.execute(
            "SELECT error FROM harvest_chunks WHERE status = 'error'"
        ).fetchall()
        assert len(errored) == 1 and "boom" in errored[0]["error"]


def test_failed_chunks_are_retried_on_the_next_run(stac_server, db_path):
    with Database(db_path) as db:
        from enmap_brazil.regions import build_chunks

        chunk = build_chunks(BRAZIL_BBOX, COLLECTION_START, COLLECTION_END, 12)[0]
        db.record_chunk(COLLECTION_ID, chunk, "error", error="earlier failure")
    result, _ = run_harvest(stac_server, db_path, window_months=12)
    assert result.chunks_skipped == 0, "only 'done' chunks are skipped"
    with Database(db_path) as db:
        assert db.conn.execute(
            "SELECT COUNT(*) FROM harvest_chunks WHERE status = 'error'"
        ).fetchone()[0] == 0


def test_queue_assets_filters_to_data_role(stac_server, db_path):
    run_harvest(stac_server, db_path, queue_assets=True)
    with Database(db_path) as db:
        rows = db.conn.execute("SELECT key, status FROM assets").fetchall()
    assert rows, "assets should have been queued"
    assert {r["key"] for r in rows} == {"data"}, "thumbnails must be filtered out"
    assert {r["status"] for r in rows} == {"pending"}


def test_harvest_run_is_recorded(stac_server, db_path):
    run_harvest(stac_server, db_path)
    with Database(db_path) as db:
        run = db.conn.execute("SELECT * FROM harvest_runs ORDER BY run_id DESC").fetchone()
    assert run["status"] == "ok"
    assert run["finished_at"] and run["items_seen"] == 25
