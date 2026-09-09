import json
import sqlite3

import pytest

from enmap_brazil.cli import build_parser, main, settings_from_args
from enmap_brazil.regions import BRAZIL_BBOX
from tests.fake_stac import COLLECTION_ID


def parse(argv):
    return build_parser().parse_args(argv)


def test_defaults_ask_for_ten_years_of_brazil():
    settings = settings_from_args(parse(["harvest"]))
    assert settings.bbox == BRAZIL_BBOX
    assert 9 <= (settings.end - settings.start).days / 365.25 <= 10.1


def test_named_region_and_explicit_dates():
    settings = settings_from_args(
        parse(["harvest", "--bbox", "pantanal", "--start", "2023-01-01", "--end", "2023-06-01"])
    )
    assert settings.bbox == (-59.0, -22.0, -54.5, -15.5)
    assert settings.start.year == 2023 and settings.end.month == 6


@pytest.mark.parametrize(
    "argv",
    [
        ["harvest", "--start", "not-a-date"],
        ["harvest", "--start", "2024-01-01", "--end", "2023-01-01"],
        ["harvest", "--bbox", "nonsense"],
    ],
)
def test_bad_scope_arguments_exit_cleanly(argv):
    with pytest.raises(SystemExit):
        settings_from_args(parse(argv))


def test_harvest_then_query_and_export(stac_server, tmp_path, capsys):
    db = tmp_path / "c.sqlite"
    common = ["--db", str(db), "--stac-root", stac_server.url]

    assert main([*common, "harvest", "--window-months", "12", "--rate-limit", "0"]) == 0
    out = capsys.readouterr().out
    assert "scenes total" in out

    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM scenes").fetchone()[0] == 25

    assert main([*common, "query", "by-year", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload and payload[0]["collection"] == COLLECTION_ID

    target = tmp_path / "out.geojson"
    assert main([*common, "export", str(target)]) == 0
    assert json.loads(target.read_text())["type"] == "FeatureCollection"


def test_harvest_is_idempotent_across_invocations(stac_server, tmp_path, capsys):
    db = tmp_path / "c.sqlite"
    common = ["--db", str(db), "--stac-root", stac_server.url, "harvest", "--rate-limit", "0"]
    main(common)
    capsys.readouterr()
    main(common)
    out = capsys.readouterr().out
    assert "already done" in out
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM scenes").fetchone()[0] == 25


def test_reset_chunks_forces_a_refetch(stac_server, tmp_path, capsys):
    db = tmp_path / "c.sqlite"
    common = ["--db", str(db), "--stac-root", stac_server.url]
    main([*common, "harvest", "--rate-limit", "0"])
    main([*common, "reset-chunks"])
    assert "cleared" in capsys.readouterr().out
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM harvest_chunks").fetchone()[0] == 0


def test_collections_command_lists_only_enmap(stac_server, tmp_path, capsys):
    code = main(["--db", str(tmp_path / "c.sqlite"), "--stac-root", stac_server.url, "collections"])
    out = capsys.readouterr().out
    assert code == 0
    assert COLLECTION_ID in out
    assert "TDM90_DEM" not in out


def test_stats_command_emits_json(stac_server, tmp_path, capsys):
    db = tmp_path / "c.sqlite"
    common = ["--db", str(db), "--stac-root", stac_server.url]
    main([*common, "harvest", "--rate-limit", "0"])
    capsys.readouterr()
    assert main([*common, "stats"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["scenes"] == 25 and stats["collections"] == 2


def test_query_rejects_writes(tmp_path, capsys):
    code = main(["--db", str(tmp_path / "c.sqlite"), "query", "DELETE FROM scenes"])
    assert code == 1
    assert "only SELECT" in capsys.readouterr().err


def test_export_rejects_an_unknown_format(tmp_path, capsys):
    code = main(["--db", str(tmp_path / "c.sqlite"), "export", str(tmp_path / "x.shp")])
    assert code == 1
    assert "unknown format" in capsys.readouterr().err
