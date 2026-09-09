"""Textual TUI for harvesting and browsing the Brazilian EnMAP catalogue."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from .config import Settings
from .db import CANNED_QUERIES, Database
from .download import Downloader, DownloadProgress, asset_rows
from .export import export
from .harvest import Harvester, HarvestEvent
from .regions import REGIONS, parse_bbox
from .stac import StacClient

LEVEL_STYLE = {
    "info": "",
    "warning": "yellow",
    "error": "bold red",
    "success": "green",
}


class EnmapBrazilApp(App[None]):
    """Point it at the catalogue, watch it fill a queryable SQLite file."""

    TITLE = "EnMAP Brazil"
    SUB_TITLE = "hyperspectral scene catalogue"

    CSS = """
    Screen { layout: vertical; }
    #settings { width: 48; border-right: solid $primary; padding: 0 1; }
    #settings Label { color: $text-muted; margin-top: 1; }
    #settings Input { margin-bottom: 0; }
    #buttons { height: auto; margin-top: 1; }
    #buttons Button { width: 1fr; margin-right: 1; }
    #status { height: auto; padding: 1; border-bottom: solid $primary; }
    #log { border: none; }
    .stat { color: $text-muted; }
    #query-bar { height: auto; padding: 0 1; }
    #query-bar Input { width: 1fr; }
    #query-bar Select { width: 28; }
    #browse-table, #collections-table, #downloads-table { height: 1fr; }
    """

    BINDINGS = [
        ("d", "discover", "Discover"),
        ("s", "start", "Start harvest"),
        ("x", "stop", "Stop"),
        ("r", "refresh", "Refresh stats"),
        ("e", "export_csv", "Export CSV"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.stop_event = threading.Event()
        self.harvest_running = False
        self.download_running = False
        self._counters = {"seen": 0, "new": 0, "done": 0, "total": 0}
        # UI-thread database handle; workers open their own.
        self.db = Database(settings.db_path)

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="tab-harvest"):
            with TabPane("Harvest", id="tab-harvest"):
                with Horizontal():
                    yield from self._settings_panel()
                    with Vertical():
                        yield Static(id="status")
                        yield ProgressBar(id="progress", total=100, show_eta=True)
                        yield RichLog(id="log", markup=True, wrap=True, highlight=False)
            with TabPane("Collections", id="tab-collections"):
                yield DataTable(id="collections-table", zebra_stripes=True)
            with TabPane("Browse", id="tab-browse"):
                with Horizontal(id="query-bar"):
                    yield Select(
                        [(name, name) for name in CANNED_QUERIES],
                        prompt="canned query",
                        id="canned",
                    )
                    yield Input(
                        value=CANNED_QUERIES["recent"],
                        placeholder="SELECT ... (read-only)",
                        id="sql",
                    )
                    yield Button("Run", id="run-sql", variant="primary")
                yield DataTable(id="browse-table", zebra_stripes=True)
            with TabPane("Downloads", id="tab-downloads"):
                with Horizontal(id="query-bar"):
                    yield Button("Queue assets from scenes", id="queue-assets")
                    yield Button("Download pending", id="download", variant="primary")
                    yield Button("Stop", id="stop-download", variant="warning")
                yield Static(id="download-status")
                yield DataTable(id="downloads-table", zebra_stripes=True)
        yield Footer()

    def _settings_panel(self) -> ComposeResult:
        s = self.settings
        with VerticalScroll(id="settings"):
            yield Label("STAC root")
            yield Input(value=s.stac_root, id="stac-root")
            yield Label("Database")
            yield Input(value=str(s.db_path), id="db-path", disabled=True)
            yield Label("Region")
            yield Select(
                [(name, name) for name in REGIONS],
                value="brazil",
                allow_blank=False,
                id="region",
            )
            yield Label("Bounding box (minx,miny,maxx,maxy)")
            yield Input(value=",".join(str(v) for v in s.bbox), id="bbox")
            yield Label("Start date (YYYY-MM-DD)")
            yield Input(value=s.start.strftime("%Y-%m-%d"), id="start")
            yield Label("End date (YYYY-MM-DD)")
            yield Input(value=s.end.strftime("%Y-%m-%d"), id="end")
            yield Label("Window (months)")
            yield Input(value=str(s.window_months), id="window", type="integer")
            yield Label("Page size")
            yield Input(value=str(s.page_limit), id="page-limit", type="integer")
            yield Checkbox("Resume finished chunks", value=s.resume, id="resume")
            yield Checkbox("Queue product assets", value=s.queue_assets, id="queue")
            with Horizontal(id="buttons"):
                yield Button("Discover", id="discover")
                yield Button("Start", id="start-harvest", variant="success")
                yield Button("Stop", id="stop-harvest", variant="warning")

    # -- lifecycle ---------------------------------------------------------
    def on_mount(self) -> None:
        self.query_one("#collections-table", DataTable).add_columns(
            "collection", "from", "to", "scenes", "title"
        )
        self.query_one("#downloads-table", DataTable).add_columns(
            "status", "item", "asset", "size", "path"
        )
        self.log_line(f"database: {self.settings.db_path}", "success")
        self.log_line(
            "Press [b]d[/b] to discover EnMAP collections, then [b]s[/b] to harvest. "
            "The requested range is clamped to what the catalogue actually holds.",
        )
        self.refresh_stats()
        self.refresh_collections()

    def on_unmount(self) -> None:
        self.stop_event.set()
        try:
            self.db.close()
        except Exception:  # pragma: no cover - shutdown best effort
            pass

    # -- helpers -----------------------------------------------------------
    def log_line(self, message: str, level: str = "info") -> None:
        style = LEVEL_STYLE.get(level, "")
        widget = self.query_one("#log", RichLog)
        stamp = datetime.now().strftime("%H:%M:%S")
        widget.write(f"[dim]{stamp}[/dim] [{style}]{message}[/]" if style else f"[dim]{stamp}[/dim] {message}")

    def refresh_stats(self) -> None:
        stats = self.db.stats()
        chunks = stats.get("chunks") or {}
        lines = [
            f"[b]{stats['scenes']:,}[/b] scenes   "
            f"[b]{stats['collections']}[/b] collections   "
            f"{stats['db_size_mb']} MiB on disk",
            f"[dim]coverage:[/dim] {stats.get('first_scene') or '—'} → {stats.get('last_scene') or '—'}",
            "[dim]chunks:[/dim] "
            + (", ".join(f"{k}={v}" for k, v in sorted(chunks.items())) or "none yet"),
            f"[dim]run:[/dim] seen={self._counters['seen']:,} new={self._counters['new']:,} "
            f"chunks={self._counters['done']}/{self._counters['total']}",
        ]
        self.query_one("#status", Static).update("\n".join(lines))

    def refresh_collections(self) -> None:
        table = self.query_one("#collections-table", DataTable)
        table.clear()
        counts = {
            r["collection"]: r["scenes"]
            for r in self.db.conn.execute(
                "SELECT collection, COUNT(*) AS scenes FROM scenes GROUP BY collection"
            ).fetchall()
        }
        for row in self.db.list_collections():
            table.add_row(
                row["id"],
                (row["temporal_start"] or "—")[:10],
                (row["temporal_end"] or "open")[:10],
                str(counts.get(row["id"], 0)),
                (row["title"] or "")[:60],
            )

    def current_settings(self) -> Settings:
        """Read the form back into a Settings object, validating as we go."""
        get = lambda wid: self.query_one(f"#{wid}", Input).value.strip()  # noqa: E731
        bbox = parse_bbox(get("bbox"))
        start = _parse_date(get("start"), "start date")
        end = _parse_date(get("end"), "end date")
        if end <= start:
            raise ValueError("end date must be after start date")
        return self.settings.with_(
            stac_root=get("stac-root") or self.settings.stac_root,
            bbox=bbox,
            start=start,
            end=end,
            window_months=max(1, int(get("window") or 3)),
            page_limit=max(1, int(get("page-limit") or 100)),
            resume=self.query_one("#resume", Checkbox).value,
            queue_assets=self.query_one("#queue", Checkbox).value,
        )

    # -- actions -----------------------------------------------------------
    def action_discover(self) -> None:
        if self.harvest_running:
            self.notify("a harvest is already running", severity="warning")
            return
        self.log_line("discovering collections…")
        self.run_worker(self._discover_worker, thread=True, name="discover")

    def action_start(self) -> None:
        if self.harvest_running:
            self.notify("already harvesting", severity="warning")
            return
        try:
            settings = self.current_settings()
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        self.settings = settings
        self.stop_event = threading.Event()
        self.harvest_running = True
        self._counters = {"seen": 0, "new": 0, "done": 0, "total": 0}
        self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        self.log_line(
            f"harvest {settings.start:%Y-%m-%d} → {settings.end:%Y-%m-%d} "
            f"over bbox {settings.bbox}",
            "success",
        )
        self.run_worker(self._harvest_worker, thread=True, name="harvest")

    def action_stop(self) -> None:
        if not (self.harvest_running or self.download_running):
            return
        self.stop_event.set()
        self.log_line("stop requested — finishing the current page…", "warning")

    def action_refresh(self) -> None:
        self.refresh_stats()
        self.refresh_collections()

    def action_export_csv(self) -> None:
        target = Path(self.settings.db_path).with_suffix(".csv")
        try:
            path, count = export(self.db, target, "csv")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.notify(f"export failed: {exc}", severity="error")
            return
        self.log_line(f"exported {count:,} scenes to {path}", "success")
        self.notify(f"exported {count:,} scenes to {path}")

    # -- buttons -----------------------------------------------------------
    @on(Button.Pressed, "#discover")
    def _btn_discover(self) -> None:
        self.action_discover()

    @on(Button.Pressed, "#start-harvest")
    def _btn_start(self) -> None:
        self.action_start()

    @on(Button.Pressed, "#stop-harvest")
    def _btn_stop(self) -> None:
        self.action_stop()

    @on(Select.Changed, "#region")
    def _region_changed(self, event: Select.Changed) -> None:
        if event.value in REGIONS:
            self.query_one("#bbox", Input).value = ",".join(
                str(v) for v in REGIONS[str(event.value)]
            )

    @on(Select.Changed, "#canned")
    def _canned_changed(self, event: Select.Changed) -> None:
        if event.value in CANNED_QUERIES:
            self.query_one("#sql", Input).value = CANNED_QUERIES[str(event.value)]
            self._run_query()

    @on(Button.Pressed, "#run-sql")
    @on(Input.Submitted, "#sql")
    def _run_query(self) -> None:
        sql = self.query_one("#sql", Input).value
        table = self.query_one("#browse-table", DataTable)
        try:
            headers, rows = self.db.run_sql(sql, limit=1000)
        except Exception as exc:  # noqa: BLE001 - bad SQL is user input
            self.notify(str(exc), severity="error")
            return
        table.clear(columns=True)
        if headers:
            table.add_columns(*headers)
            for row in rows:
                table.add_row(*["" if v is None else str(v) for v in row])
        self.notify(f"{len(rows)} row(s)")

    @on(Button.Pressed, "#queue-assets")
    def _queue_assets(self) -> None:
        self.run_worker(self._queue_assets_worker, thread=True, name="queue")

    @on(Button.Pressed, "#download")
    def _start_download(self) -> None:
        if self.download_running:
            self.notify("download already running", severity="warning")
            return
        self.download_running = True
        self.stop_event = threading.Event()
        self.run_worker(self._download_worker, thread=True, name="download")

    @on(Button.Pressed, "#stop-download")
    def _stop_download(self) -> None:
        self.stop_event.set()

    # -- workers (run off the UI thread) -----------------------------------
    def _discover_worker(self) -> None:
        settings = self.settings
        try:
            with StacClient(
                settings.stac_root,
                timeout=settings.timeout,
                max_retries=settings.max_retries,
                rate_limit=settings.rate_limit,
            ) as client:
                found = client.collections()
                enmap = client.enmap_collections(collections=found)
            with Database(settings.db_path) as db:
                db.upsert_collections(found)
        except Exception as exc:  # noqa: BLE001 - reported in the log pane
            self.call_from_thread(self.log_line, f"discovery failed: {exc}", "error")
            return
        names = ", ".join(c.id for c in enmap) or "none"
        self.call_from_thread(
            self.log_line,
            f"{len(found)} collections in catalogue; EnMAP matches: {names}",
            "success",
        )
        for coll in enmap:
            self.call_from_thread(
                self.log_line,
                f"  {coll.id}: {_fmt(coll.temporal_start)} → {_fmt(coll.temporal_end)}",
            )
        self.call_from_thread(self.action_refresh)

    def _harvest_worker(self) -> None:
        settings = self.settings
        try:
            with Database(settings.db_path) as db, StacClient(
                settings.stac_root,
                timeout=settings.timeout,
                max_retries=settings.max_retries,
                rate_limit=settings.rate_limit,
            ) as client:
                Harvester(
                    settings,
                    db,
                    client,
                    on_event=lambda event: self.call_from_thread(self._on_event, event),
                    stop_event=self.stop_event,
                ).run()
        except Exception as exc:  # noqa: BLE001 - reported in the log pane
            self.call_from_thread(self.log_line, f"harvest crashed: {exc}", "error")
        finally:
            self.harvest_running = False
            self.call_from_thread(self.action_refresh)

    def _queue_assets_worker(self) -> None:
        settings = self.settings
        wanted = {r.lower() for r in settings.asset_roles}
        queued = 0
        try:
            with Database(settings.db_path) as db:
                batch: list[dict[str, Any]] = []
                for scene in db.conn.execute(
                    "SELECT collection, item_id, assets_json FROM scenes"
                ):
                    try:
                        assets = json.loads(scene["assets_json"] or "{}")
                    except ValueError:
                        continue
                    batch += asset_rows(
                        scene["collection"], scene["item_id"], assets, wanted
                    )
                    if len(batch) >= 500:
                        db.queue_assets(batch)
                        queued += len(batch)
                        batch = []
                if batch:
                    db.queue_assets(batch)
                    queued += len(batch)
        except Exception as exc:  # noqa: BLE001 - reported in the log pane
            self.call_from_thread(self.log_line, f"queueing failed: {exc}", "error")
            return
        self.call_from_thread(self.log_line, f"queued {queued} asset(s)", "success")
        self.call_from_thread(self._refresh_downloads)

    def _download_worker(self) -> None:
        settings = self.settings
        try:
            with Database(settings.db_path) as db:
                downloader = Downloader(
                    db,
                    settings.download_dir,
                    auth=settings.auth(),
                    on_progress=lambda p: self.call_from_thread(self._on_download, p),
                    stop_event=self.stop_event,
                )
                try:
                    tally = downloader.run(limit=1000)
                finally:
                    downloader.close()
            self.call_from_thread(
                self.log_line,
                f"downloads: {tally['done']} done, {tally['error']} failed",
                "success" if not tally["error"] else "warning",
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.log_line, f"download failed: {exc}", "error")
        finally:
            self.download_running = False
            self.call_from_thread(self._refresh_downloads)

    # -- callbacks from workers -------------------------------------------
    def _on_event(self, event: HarvestEvent) -> None:
        data = event.data
        if event.kind == "plan":
            self._counters["total"] = int(data.get("total", 0))
            self.query_one("#progress", ProgressBar).update(
                total=self._counters["total"] or None, progress=0
            )
        elif event.kind in ("chunk-done",):
            self._counters["done"] = int(data.get("done", self._counters["done"]))
            self.query_one("#progress", ProgressBar).update(progress=self._counters["done"])
        elif event.kind == "page":
            self._counters["seen"] = int(data.get("items_seen", self._counters["seen"]))
            self._counters["new"] = int(data.get("items_new", self._counters["new"]))
        if event.message and not (event.kind == "chunk-done" and data.get("skipped")):
            self.log_line(event.message, event.level)
        if event.kind in ("plan", "chunk-done", "finished"):
            self.refresh_stats()
        if event.kind == "finished":
            self.refresh_collections()

    def _on_download(self, progress: DownloadProgress) -> None:
        total = f"{progress.total / 1_048_576:.1f} MiB" if progress.total else "unknown size"
        self.query_one("#download-status", Static).update(
            f"{progress.item_id} / {progress.key}: "
            f"{progress.downloaded / 1_048_576:.1f} MiB of {total}"
        )

    def _refresh_downloads(self) -> None:
        table = self.query_one("#downloads-table", DataTable)
        table.clear()
        for row in self.db.conn.execute(
            "SELECT status, item_id, key, file_size, local_path FROM assets "
            "ORDER BY updated_at DESC LIMIT 500"
        ):
            size = row["file_size"]
            table.add_row(
                row["status"],
                row["item_id"],
                row["key"],
                f"{size / 1_048_576:.1f} MiB" if size else "—",
                row["local_path"] or "—",
            )
        self.refresh_stats()


def _parse_date(text: str, label: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{label} must look like YYYY-MM-DD (got {text!r})") from exc


def _fmt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "open"


def run_tui(settings: Settings) -> None:
    EnmapBrazilApp(settings).run()
