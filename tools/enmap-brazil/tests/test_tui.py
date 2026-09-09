"""Headless tests for the Textual app, driven by Textual's own pilot."""

import asyncio
from datetime import datetime, timezone

import pytest

from enmap_brazil.config import Settings
from enmap_brazil.db import Database
from enmap_brazil.regions import BRAZIL_BBOX
from enmap_brazil.tui import EnmapBrazilApp
from tests.fake_stac import COLLECTION_ID

from textual.widgets import Button, DataTable, Input, ProgressBar, Select, Static

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def settings_for(server, db_path, **kwargs):
    base = dict(
        stac_root=server.url,
        db_path=db_path,
        bbox=BRAZIL_BBOX,
        start=datetime(2015, 1, 1, tzinfo=timezone.utc),
        end=datetime(2025, 9, 1, tzinfo=timezone.utc),
        window_months=12,
        rate_limit=0.0,
        page_limit=10,
    )
    base.update(kwargs)
    return Settings(**base)


def drive(scenario):
    """Run an async pilot scenario from a sync test."""
    return asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_app_mounts_and_shows_the_controls(stac_server, db_path):
    app = EnmapBrazilApp(settings_for(stac_server, db_path))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#bbox", Input).value.startswith("-73.99")
            assert app.query_one("#start", Input).value == "2015-01-01"
            assert {b.id for b in app.query(Button)} >= {
                "discover", "start-harvest", "stop-harvest", "run-sql"
            }

    drive(scenario)


def test_discover_populates_the_collections_tab(stac_server, db_path):
    app = EnmapBrazilApp(settings_for(stac_server, db_path))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_discover()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#collections-table", DataTable)
            ids = [str(table.get_cell_at((r, 0))) for r in range(table.row_count)]
            assert COLLECTION_ID in ids

    drive(scenario)


def test_harvest_from_the_ui_fills_the_database(stac_server, db_path):
    app = EnmapBrazilApp(settings_for(stac_server, db_path))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_start()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.harvest_running is False
            assert app._counters["new"] == stac_server.item_count
            progress = app.query_one("#progress", ProgressBar)
            assert progress.progress == progress.total
            status = app.query_one("#status", Static).render()
            assert "25" in str(status)

    drive(scenario)
    with Database(db_path) as db:
        assert db.scene_count() == stac_server.item_count


def test_browse_tab_runs_a_canned_query(stac_server, db_path):
    with Database(db_path) as db:
        from enmap_brazil.db import Scene
        from tests.fake_stac import make_item

        db.upsert_scenes(
            [Scene.from_item(make_item(i, datetime(2023, 1, 1 + i, tzinfo=timezone.utc))) for i in range(3)]
        )

    app = EnmapBrazilApp(settings_for(stac_server, db_path))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#sql", Input).value = "SELECT item_id, datetime FROM scenes"
            app._run_query()
            await pilot.pause()
            table = app.query_one("#browse-table", DataTable)
            assert table.row_count == 3
            assert [str(c.label) for c in table.columns.values()] == ["item_id", "datetime"]

    drive(scenario)


def test_browse_tab_refuses_a_write_query(stac_server, db_path):
    app = EnmapBrazilApp(settings_for(stac_server, db_path))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#sql", Input).value = "DELETE FROM scenes"
            app._run_query()
            await pilot.pause()
            assert app.query_one("#browse-table", DataTable).row_count == 0

    drive(scenario)


def test_region_select_rewrites_the_bbox(stac_server, db_path):
    app = EnmapBrazilApp(settings_for(stac_server, db_path))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#region", Select).value = "pantanal"
            await pilot.pause()
            assert app.query_one("#bbox", Input).value == "-59.0,-22.0,-54.5,-15.5"

    drive(scenario)


def test_invalid_form_input_is_rejected_without_starting_a_harvest(stac_server, db_path):
    app = EnmapBrazilApp(settings_for(stac_server, db_path))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#start", Input).value = "yesterday"
            app.action_start()
            await pilot.pause()
            assert app.harvest_running is False

            app.query_one("#start", Input).value = "2024-01-01"
            app.query_one("#end", Input).value = "2023-01-01"
            app.action_start()
            await pilot.pause()
            assert app.harvest_running is False

    drive(scenario)


def test_export_action_writes_a_csv(stac_server, db_path, tmp_path):
    with Database(db_path) as db:
        from enmap_brazil.db import Scene
        from tests.fake_stac import make_item

        db.upsert_scenes([Scene.from_item(make_item(0, datetime(2023, 1, 1, tzinfo=timezone.utc)))])

    app = EnmapBrazilApp(settings_for(stac_server, db_path))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_export_csv()
            await pilot.pause()

    drive(scenario)
    assert db_path.with_suffix(".csv").exists()
