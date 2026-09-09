import csv
import json
from datetime import datetime, timezone

import pytest

from enmap_brazil.db import Database, Scene
from enmap_brazil.export import export, rows_to_text
from tests.fake_stac import make_item


@pytest.fixture
def filled_db(db_path):
    with Database(db_path) as db:
        db.upsert_scenes(
            [
                Scene.from_item(make_item(i, datetime(2023, 1, 1 + i, tzinfo=timezone.utc), lon=-60 + i))
                for i in range(4)
            ]
        )
        yield db


def test_csv_export(filled_db, tmp_path):
    path, count = export(filled_db, tmp_path / "scenes.csv", "csv")
    assert count == 4
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 4
    assert "geometry_json" not in rows[0], "CSV drops the bulky geometry column"
    assert rows[0]["collection"] == "ENMAP_HSI_L2A"
    assert float(rows[0]["minx"]) == -60


def test_geojson_export_is_valid_and_keeps_geometry(filled_db, tmp_path):
    path, count = export(filled_db, tmp_path / "scenes.geojson", "geojson")
    doc = json.loads(path.read_text())
    assert doc["type"] == "FeatureCollection"
    assert len(doc["features"]) == count == 4
    feature = doc["features"][0]
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["item_id"]
    assert "geometry_json" not in feature["properties"]


def test_geojson_falls_back_to_the_bbox_when_geometry_is_missing(db_path, tmp_path):
    item = make_item(0, datetime(2023, 1, 1, tzinfo=timezone.utc), lon=-10, lat=-20)
    del item["geometry"]
    with Database(db_path) as db:
        db.upsert_scenes([Scene.from_item(item)])
        path, _ = export(db, tmp_path / "s.geojson", "geojson")
    ring = json.loads(path.read_text())["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == [-10, -20] and ring[2] == [-9.7, -19.7]


def test_ndjson_export_is_one_object_per_line(filled_db, tmp_path):
    path, count = export(filled_db, tmp_path / "scenes.ndjson", "ndjson")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == count == 4
    assert json.loads(lines[0])["item_id"]


def test_export_honours_custom_sql_and_limit(filled_db, tmp_path):
    path, count = export(
        filled_db, tmp_path / "few.csv", "csv",
        sql="SELECT item_id, datetime FROM scenes ORDER BY datetime", limit=2,
    )
    assert count == 2
    rows = list(csv.reader(path.open()))
    assert rows[0] == ["item_id", "datetime"]


def test_unknown_format_is_rejected(filled_db, tmp_path):
    with pytest.raises(ValueError, match="unknown format"):
        export(filled_db, tmp_path / "x.shp", "shapefile")


def test_export_creates_missing_directories(filled_db, tmp_path):
    path, count = export(filled_db, tmp_path / "nested" / "dir" / "s.csv", "csv")
    assert path.exists() and count == 4


def test_rows_to_text_renders_none_as_blank():
    text = rows_to_text(["a", "b"], [(1, None)])
    assert "a" in text and "1" in text
    assert "None" not in text
