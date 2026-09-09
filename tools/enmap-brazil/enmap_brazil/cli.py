"""Command line entry point. ``enmap-br`` with no arguments opens the TUI."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .config import DEFAULT_DB, DEFAULT_DOWNLOAD_DIR, DEFAULT_YEARS, Settings, utcnow
from .db import CANNED_QUERIES, Database
from .download import Downloader
from .export import FORMATS, export, rows_to_text
from .harvest import HarvestEvent, Harvester
from .regions import REGIONS, parse_bbox
from .stac import DEFAULT_STAC_ROOT, StacClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enmap-br",
        description=(
            "Download the EnMAP hyperspectral scene catalogue for Brazil into a "
            "queryable SQLite database. Run without a subcommand for the TUI."
        ),
    )
    parser.add_argument("--version", action="version", version=f"enmap-brazil {__version__}")
    _add_global_args(parser)

    # The same globals are accepted after the subcommand too, so both
    # "enmap-br --db X harvest" and "enmap-br harvest --db X" work. SUPPRESS
    # keeps the second copy from overwriting a value given in the first
    # position with its own default.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_args(common, suppress=True)

    sub = parser.add_subparsers(dest="command")

    tui = sub.add_parser("tui", parents=[common], help="launch the interactive terminal UI (default)")
    _add_scope_args(tui)

    harvest = sub.add_parser("harvest", parents=[common], help="run a harvest headlessly")
    _add_scope_args(harvest)
    harvest.add_argument("--no-resume", action="store_true", help="re-fetch chunks already marked done")
    harvest.add_argument("--queue-assets", action="store_true", help="queue product assets for download")
    harvest.add_argument("--rate-limit", type=float, default=0.2, help="min seconds between requests")
    harvest.add_argument("--page-limit", type=int, default=100, help="items per page")
    harvest.add_argument("--quiet", action="store_true", help="only print the final summary")

    sub.add_parser("collections", parents=[common], help="list the catalogue's EnMAP collections")
    sub.add_parser("stats", parents=[common], help="summarise what the local database holds")

    query = sub.add_parser("query", parents=[common], help="run a read-only SQL query or a canned one")
    query.add_argument("sql", nargs="?", help=f"SQL, or one of: {', '.join(CANNED_QUERIES)}")
    query.add_argument("--limit", type=int, default=50)
    query.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    exp = sub.add_parser("export", parents=[common], help="export scenes to csv/geojson/ndjson/parquet")
    exp.add_argument("destination", type=Path)
    exp.add_argument("--format", choices=FORMATS, default=None, help="default: from the file suffix")
    exp.add_argument("--sql", help="custom SELECT to export instead of all scenes")
    exp.add_argument("--limit", type=int)

    download = sub.add_parser("download", parents=[common], help="download queued product assets")
    download.add_argument("--dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    download.add_argument("--limit", type=int, default=10, help="max assets this run")
    download.add_argument("--no-hash", action="store_true", help="skip sha256 verification")

    reset = sub.add_parser("reset-chunks", parents=[common], help="forget harvest progress so the next run refetches")
    reset.add_argument("--collection", help="limit the reset to one collection")

    return parser


def _add_global_args(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Options valid both before and after the subcommand."""
    parser.add_argument(
        "--db", type=Path,
        default=argparse.SUPPRESS if suppress else DEFAULT_DB,
        help="SQLite file (default: %(default)s)" if not suppress else argparse.SUPPRESS,
    )
    parser.add_argument(
        "--stac-root",
        default=argparse.SUPPRESS if suppress else DEFAULT_STAC_ROOT,
        help="STAC API root" if not suppress else argparse.SUPPRESS,
    )
    parser.add_argument(
        "-v", "--verbose", action="count",
        default=argparse.SUPPRESS if suppress else 0,
        help="repeat for debug logging" if not suppress else argparse.SUPPRESS,
    )


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bbox",
        default="brazil",
        help=f"'minx,miny,maxx,maxy' or a named region ({', '.join(REGIONS)})",
    )
    parser.add_argument("--start", help=f"YYYY-MM-DD (default: {DEFAULT_YEARS} years ago)")
    parser.add_argument("--end", help="YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--collection", action="append", dest="collections", default=[],
        help="collection id; repeatable. Default: auto-detect EnMAP collections",
    )
    parser.add_argument("--window-months", type=int, default=3, help="time slice per chunk")
    parser.add_argument("--grid-deg", type=float, help="also split the bbox into tiles this wide")


def _parse_date(text: str | None, fallback: datetime) -> datetime:
    if not text:
        return fallback
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"error: dates must look like YYYY-MM-DD (got {text!r})") from exc


def settings_from_args(args: argparse.Namespace) -> Settings:
    end = _parse_date(getattr(args, "end", None), utcnow())
    start = _parse_date(
        getattr(args, "start", None), end - timedelta(days=365 * DEFAULT_YEARS)
    )
    if end <= start:
        raise SystemExit("error: --end must be after --start")
    try:
        bbox = parse_bbox(getattr(args, "bbox", None) or "brazil")
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    return Settings(
        stac_root=args.stac_root,
        db_path=args.db,
        collections=list(getattr(args, "collections", []) or []),
        bbox=bbox,
        start=start,
        end=end,
        window_months=max(1, getattr(args, "window_months", 3)),
        grid_deg=getattr(args, "grid_deg", None),
        page_limit=getattr(args, "page_limit", 100),
        rate_limit=getattr(args, "rate_limit", 0.2),
        resume=not getattr(args, "no_resume", False),
        queue_assets=getattr(args, "queue_assets", False),
        download_dir=getattr(args, "dir", DEFAULT_DOWNLOAD_DIR),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
    )
    command = args.command or "tui"

    if command == "tui":
        from .tui import run_tui  # imported lazily so the CLI works without textual

        run_tui(settings_from_args(args))
        return 0
    if command == "harvest":
        return cmd_harvest(args)
    if command == "collections":
        return cmd_collections(args)
    if command == "stats":
        return cmd_stats(args)
    if command == "query":
        return cmd_query(args)
    if command == "export":
        return cmd_export(args)
    if command == "download":
        return cmd_download(args)
    if command == "reset-chunks":
        return cmd_reset(args)
    parser.error(f"unknown command {command!r}")
    return 2


def cmd_harvest(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    stop = threading.Event()

    def on_event(event: HarvestEvent) -> None:
        if args.quiet and event.kind not in ("finished",):
            return
        if event.kind == "page" and not args.verbose:
            return
        if event.message:
            print(event.message, file=sys.stderr if event.level == "error" else sys.stdout, flush=True)

    with Database(settings.db_path) as db, StacClient(
        settings.stac_root,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
        rate_limit=settings.rate_limit,
    ) as client:
        result = Harvester(settings, db, client, on_event=on_event, stop_event=stop).run()
        print(
            f"\n{result.status}: {result.items_seen} items seen, {result.items_new} new, "
            f"{result.chunks_done}/{result.chunks_total} chunks "
            f"({result.chunks_skipped} already done, {result.chunks_failed} failed)"
        )
        print(f"database: {settings.db_path} ({db.scene_count():,} scenes total)")
    return 0 if result.status in ("ok", "empty") else 1


def cmd_collections(args: argparse.Namespace) -> int:
    with StacClient(args.stac_root) as client:
        found = client.collections()
        enmap = client.enmap_collections(collections=found)
        with Database(args.db) as db:
            db.upsert_collections(found)
    if not enmap:
        print("no EnMAP collections found; pass --collection explicitly", file=sys.stderr)
        return 1
    for coll in enmap:
        lo = coll.temporal_start.strftime("%Y-%m-%d") if coll.temporal_start else "?"
        hi = coll.temporal_end.strftime("%Y-%m-%d") if coll.temporal_end else "open"
        print(f"{coll.id}\n    {lo} → {hi}\n    {coll.title or coll.description[:80]}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with Database(args.db) as db:
        stats = db.stats()
    print(json.dumps(stats, indent=2, default=str))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    sql = args.sql or "recent"
    sql = CANNED_QUERIES.get(sql, sql)
    with Database(args.db) as db:
        try:
            headers, rows = db.run_sql(sql, limit=args.limit)
        except Exception as exc:  # noqa: BLE001 - user-supplied SQL
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps([dict(zip(headers, row)) for row in rows], indent=2, default=str))
    else:
        print(rows_to_text(headers, rows))
        print(f"\n{len(rows)} row(s)")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    fmt = args.format or args.destination.suffix.lstrip(".").lower() or "csv"
    if fmt == "json":
        fmt = "geojson"
    with Database(args.db) as db:
        try:
            path, count = export(db, args.destination, fmt, sql=args.sql, limit=args.limit)
        except Exception as exc:  # noqa: BLE001 - bad format or missing pyarrow
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"wrote {count:,} rows to {path}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    if settings.auth() is None:
        print(
            "note: no credentials found. EnMAP products need an EOWEB/DLR account; "
            "set ENMAP_BR_USER and ENMAP_BR_PASSWORD if downloads come back 401.",
            file=sys.stderr,
        )
    with Database(settings.db_path) as db:
        downloader = Downloader(
            db,
            args.dir,
            auth=settings.auth(),
            verify_hash=not args.no_hash,
            on_progress=lambda p: print(
                f"\r{p.item_id}/{p.key}: {p.downloaded / 1048576:.1f} MiB", end="", flush=True
            ),
        )
        try:
            tally = downloader.run(limit=args.limit)
        finally:
            downloader.close()
    print(f"\n{tally['done']} downloaded, {tally['error']} failed")
    return 0 if not tally["error"] else 1


def cmd_reset(args: argparse.Namespace) -> int:
    with Database(args.db) as db:
        removed = db.reset_chunks(args.collection)
    print(f"cleared {removed} chunk record(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
