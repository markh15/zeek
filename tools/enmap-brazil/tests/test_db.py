import json

import pytest

from enmap_brazil.db import Database, Scene
from tests.fake_stac import make_item
from datetime import datetime, timezone


def test_scene_from_item_flattens_the_columns_we_index():
    item = make_item(3, datetime(2023, 5, 6, 13, 30, tzinfo=timezone.utc), lon=-60, lat=-10)
    scene = Scene.from_item(item)
    row = scene.row
    assert row["item_id"] == item["id"]
    assert row["collection"] == "ENMAP_HSI_L2A"
    assert row["datetime"] == "2023-05-06T13:30:00Z"
    assert (row["year"], row["month"]) == (2023, 5)
    assert (row["minx"], row["miny"]) == (-60, -10)
    assert row["cloud_cover"] == 21
    assert row["processing_level"] == "L2A"
    assert row["instruments"] == "hsi"
    assert row["centroid_lon"] == pytest.approx(-59.85)
    assert json.loads(row["properties_json"])["platform"] == "enmap"


def test_scene_falls_back_to_geometry_when_bbox_is_missing():
    item = make_item(1, datetime(2023, 1, 1, tzinfo=timezone.utc), lon=-50, lat=-20)
    del item["bbox"]
    row = Scene.from_item(item).row
    assert (row["minx"], row["miny"], row["maxx"], row["maxy"]) == (-50, -20, -49.7, -19.7)


def test_scene_survives_a_sparse_item():
    row = Scene.from_item({"id": "bare", "properties": {}}, "COLL").row
    assert row["item_id"] == "bare"
    assert row["collection"] == "COLL"
    assert row["datetime"] is None and row["minx"] is None


def test_upsert_is_idempotent_and_counts_only_new_rows(db_path):
    scenes = [
        Scene.from_item(make_item(i, datetime(2023, 1, 1, tzinfo=timezone.utc)))
        for i in range(5)
    ]
    with Database(db_path) as db:
        seen, new = db.upsert_scenes(scenes)
        assert (seen, new) == (5, 5)
        seen, new = db.upsert_scenes(scenes)
        assert (seen, new) == (5, 0), "re-harvesting must not duplicate rows"
        assert db.scene_count() == 5


def test_rtree_index_tracks_inserts_updates_and_deletes(db_path):
    with Database(db_path) as db:
        db.upsert_scenes([Scene.from_item(make_item(0, datetime(2023, 1, 1, tzinfo=timezone.utc), lon=-60, lat=-10))])
        rtree = lambda: db.conn.execute("SELECT COUNT(*) AS n FROM scenes_bbox").fetchone()["n"]  # noqa: E731
        assert rtree() == 1

        assert len(db.query_scenes(bbox=(-61, -11, -59, -9))) == 1
        assert len(db.query_scenes(bbox=(0, 0, 10, 10))) == 0, "far-away bbox must not match"

        db.conn.execute("UPDATE scenes SET minx = 10, maxx = 11, miny = 10, maxy = 11")
        db.conn.commit()
        assert len(db.query_scenes(bbox=(-61, -11, -59, -9))) == 0
        assert len(db.query_scenes(bbox=(9, 9, 12, 12))) == 1

        db.conn.execute("DELETE FROM scenes")
        db.conn.commit()
        assert rtree() == 0


def test_query_scenes_filters(db_path):
    with Database(db_path) as db:
        db.upsert_scenes(
            [
                Scene.from_item(make_item(i, datetime(2023, 1, 1 + i, tzinfo=timezone.utc)))
                for i in range(5)
            ]
        )
        assert len(db.query_scenes(start="2023-01-03T00:00:00Z")) == 3
        assert len(db.query_scenes(end="2023-01-02T00:00:00Z")) == 2
        assert len(db.query_scenes(collection="nope")) == 0
        assert all(r["cloud_cover"] <= 30 for r in db.query_scenes(max_cloud=30))
        assert len(db.query_scenes(limit=2)) == 2


def test_run_sql_is_read_only(db_path):
    with Database(db_path) as db:
        headers, rows = db.run_sql("SELECT 1 AS one")
        assert (headers, rows) == (["one"], [(1,)])
        for bad in (
            "DELETE FROM scenes",
            "DROP TABLE scenes",
            "UPDATE scenes SET item_id = 'x'",
            "SELECT 1; DROP TABLE scenes",
            "INSERT INTO meta VALUES ('a','b')",
        ):
            with pytest.raises(ValueError):
                db.run_sql(bad)
        assert db.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='scenes'").fetchone()[0] == 1


def test_chunk_bookkeeping_round_trips(db_path):
    from enmap_brazil.regions import build_chunks

    chunk = build_chunks((-70, -10, -60, 0), datetime(2023, 1, 1, tzinfo=timezone.utc), datetime(2023, 2, 1, tzinfo=timezone.utc))[0]
    with Database(db_path) as db:
        assert db.chunk_status("C", chunk.key) is None
        db.record_chunk("C", chunk, "running")
        assert db.chunk_status("C", chunk.key) == "running"
        db.record_chunk("C", chunk, "done", items=12, pages=2)
        assert db.chunk_status("C", chunk.key) == "done"
        assert db.reset_chunks() == 1
        assert db.chunk_status("C", chunk.key) is None


def test_migrate_is_reentrant(db_path):
    with Database(db_path) as db:
        db.migrate()
        db.migrate()
        assert db.get_meta("schema_version") == "1"


def test_stats_reports_coverage(db_path):
    with Database(db_path) as db:
        db.upsert_scenes(
            [Scene.from_item(make_item(i, datetime(2023, 1, 1 + i, tzinfo=timezone.utc))) for i in range(3)]
        )
        stats = db.stats()
    assert stats["scenes"] == 3
    assert stats["first_scene"] == "2023-01-01T00:00:00Z"
    assert stats["per_collection"][0]["collection"] == "ENMAP_HSI_L2A"


def test_views_are_queryable(db_path):
    with Database(db_path) as db:
        db.upsert_scenes(
            [Scene.from_item(make_item(i, datetime(2023, 3, 1, tzinfo=timezone.utc))) for i in range(4)]
        )
        for view in ("v_scenes", "v_scenes_by_month", "v_scenes_by_year"):
            assert db.conn.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0] >= 1
