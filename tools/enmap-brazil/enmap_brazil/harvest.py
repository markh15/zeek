"""The harvest engine: walk the catalogue chunk by chunk, store as we go.

Work is split into (spatial tile x time window) chunks whose completion is
recorded in the database. That makes a Brazil-wide, decade-long sweep resumable:
kill it at any point and the next run picks up at the first chunk that never
reached ``done``.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from .config import Settings
from .db import Database, Scene
from .download import asset_rows
from .regions import Chunk, build_chunks, clamp_range
from .stac import Collection, StacClient, StacError

log = logging.getLogger(__name__)

EventHandler = Callable[["HarvestEvent"], None]


@dataclass
class HarvestEvent:
    kind: str  # log | plan | chunk-start | chunk-done | page | finished
    message: str = ""
    level: str = "info"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarvestResult:
    status: str = "ok"
    items_seen: int = 0
    items_new: int = 0
    chunks_total: int = 0
    chunks_done: int = 0
    chunks_skipped: int = 0
    chunks_failed: int = 0
    collections: list[str] = field(default_factory=list)
    error: str | None = None


class Harvester:
    """Drives a full harvest. Safe to run on a worker thread.

    ``db`` must have been opened on the same thread that calls :meth:`run`.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        client: StacClient,
        on_event: EventHandler | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.on_event = on_event or (lambda event: None)
        self.stop_event = stop_event or threading.Event()

    # -- helpers -----------------------------------------------------------
    def _emit(self, kind: str, message: str = "", level: str = "info", **data: Any) -> None:
        try:
            self.on_event(HarvestEvent(kind=kind, message=message, level=level, data=data))
        except Exception:  # a broken UI callback must not kill the harvest
            log.exception("event handler raised")

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    # -- planning ----------------------------------------------------------
    def resolve_collections(self) -> list[Collection]:
        """Pick the collections to harvest and cache them in the database."""
        found = self.client.collections()
        by_id = {c.id: c for c in found}
        if self.settings.collections:
            missing = [c for c in self.settings.collections if c not in by_id]
            if missing:
                raise StacError(
                    f"collections not found in {self.settings.stac_root}: {', '.join(missing)}"
                )
            chosen = [by_id[c] for c in self.settings.collections]
        else:
            chosen = self.client.enmap_collections(collections=found)
            if not chosen:
                raise StacError(
                    "no EnMAP-looking collections in the catalogue; pass explicit "
                    "collection ids with --collection"
                )
        self.db.upsert_collections(found)
        return chosen

    def plan(self, collections: Sequence[Collection]) -> list[tuple[Collection, list[Chunk]]]:
        """Build the per-collection work list, clamped to real data availability."""
        plan: list[tuple[Collection, list[Chunk]]] = []
        for coll in collections:
            window = clamp_range(self.settings.start, self.settings.end, coll.temporal_extent)
            if window is None:
                self._emit(
                    "log",
                    f"{coll.id}: requested range does not overlap its temporal extent "
                    f"({_fmt(coll.temporal_start)} .. {_fmt(coll.temporal_end)}) — skipping",
                    level="warning",
                )
                continue
            lo, hi = window
            if lo > self.settings.start or hi < self.settings.end:
                self._emit(
                    "log",
                    f"{coll.id}: clamped to {lo:%Y-%m-%d}..{hi:%Y-%m-%d} "
                    f"(catalogue holds {_fmt(coll.temporal_start)} .. {_fmt(coll.temporal_end)})",
                )
            chunks = build_chunks(
                self.settings.bbox,
                lo,
                hi,
                window_months=self.settings.window_months,
                grid_deg=self.settings.grid_deg,
            )
            plan.append((coll, chunks))
        return plan

    # -- execution ---------------------------------------------------------
    def run(self) -> HarvestResult:
        result = HarvestResult()
        run_id: int | None = None
        try:
            collections = self.resolve_collections()
            result.collections = [c.id for c in collections]
            self._emit(
                "log",
                "harvesting collections: " + ", ".join(result.collections),
            )
            plan = self.plan(collections)
            result.chunks_total = sum(len(chunks) for _, chunks in plan)
            self._emit(
                "plan",
                f"{result.chunks_total} chunks across {len(plan)} collection(s)",
                total=result.chunks_total,
                collections=result.collections,
            )
            if not result.chunks_total:
                result.status = "empty"
                self._emit("finished", "nothing to harvest", level="warning")
                return result

            run_id = self.db.start_run(
                collections=",".join(result.collections),
                bbox=",".join(str(v) for v in self.settings.bbox),
                dt_start=self.settings.start.isoformat(),
                dt_end=self.settings.end.isoformat(),
                params={
                    "window_months": self.settings.window_months,
                    "grid_deg": self.settings.grid_deg,
                    "page_limit": self.settings.page_limit,
                    "stac_root": self.settings.stac_root,
                },
            )

            for coll, chunks in plan:
                for chunk in chunks:
                    if self._stopped():
                        result.status = "stopped"
                        raise _Stopped()
                    self._process_chunk(coll, chunk, result)

            result.status = "ok"
        except _Stopped:
            result.status = "stopped"
            self._emit("log", "stopped by user", level="warning")
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            result.status = "error"
            result.error = str(exc)
            log.exception("harvest failed")
            self._emit("log", f"harvest failed: {exc}", level="error")
        finally:
            if run_id is not None:
                self.db.finish_run(
                    run_id,
                    result.status,
                    result.items_seen,
                    result.items_new,
                    result.chunks_done,
                    result.error,
                )
            self._emit(
                "finished",
                f"{result.status}: {result.items_seen} items seen, "
                f"{result.items_new} new, {result.chunks_done}/{result.chunks_total} chunks",
                level="error" if result.status == "error" else "info",
                result=result,
            )
        return result

    def _process_chunk(self, coll: Collection, chunk: Chunk, result: HarvestResult) -> None:
        if self.settings.resume and self.db.chunk_status(coll.id, chunk.key) == "done":
            result.chunks_skipped += 1
            result.chunks_done += 1
            self._emit(
                "chunk-done",
                f"{coll.id} {chunk.label}: already harvested",
                collection=coll.id,
                chunk=chunk.key,
                done=result.chunks_done,
                total=result.chunks_total,
                skipped=True,
            )
            return

        self._emit(
            "chunk-start",
            f"{coll.id} {chunk.label}",
            collection=coll.id,
            chunk=chunk.key,
            done=result.chunks_done,
            total=result.chunks_total,
        )
        self.db.record_chunk(coll.id, chunk, "running")

        items = pages = 0
        try:
            for page in self.client.iter_search(
                collections=[coll.id],
                bbox=chunk.bbox,
                datetime_range=chunk.datetime_range,
                limit=self.settings.page_limit,
                should_stop=self._stopped,
            ):
                pages += 1
                scenes = [Scene.from_item(f, coll.id) for f in page.features]
                seen, new = self.db.upsert_scenes(scenes)
                items += seen
                result.items_seen += seen
                result.items_new += new
                if self.settings.queue_assets:
                    self._queue_assets(coll.id, scenes)
                self._emit(
                    "page",
                    f"{coll.id} {chunk.label}: page {pages}, {seen} items ({new} new)",
                    collection=coll.id,
                    chunk=chunk.key,
                    page=pages,
                    items=items,
                    matched=page.matched,
                    items_seen=result.items_seen,
                    items_new=result.items_new,
                )
                if self._stopped():
                    raise _Stopped()
        except _Stopped:
            self.db.record_chunk(coll.id, chunk, "pending", items, pages, "interrupted")
            raise
        except Exception as exc:  # noqa: BLE001 - one bad chunk must not end the run
            result.chunks_failed += 1
            self.db.record_chunk(coll.id, chunk, "error", items, pages, str(exc))
            self._emit(
                "chunk-done",
                f"{coll.id} {chunk.label}: FAILED — {exc}",
                level="error",
                collection=coll.id,
                chunk=chunk.key,
                done=result.chunks_done,
                total=result.chunks_total,
                failed=True,
            )
            return

        self.db.record_chunk(coll.id, chunk, "done", items, pages)
        result.chunks_done += 1
        self._emit(
            "chunk-done",
            f"{coll.id} {chunk.label}: {items} items",
            collection=coll.id,
            chunk=chunk.key,
            done=result.chunks_done,
            total=result.chunks_total,
            items=items,
        )

    def _queue_assets(self, collection: str, scenes: Sequence[Scene]) -> None:
        wanted = {r.lower() for r in self.settings.asset_roles}
        rows: list[dict[str, Any]] = []
        for scene in scenes:
            rows += asset_rows(collection, scene.item_id, _load_assets(scene), wanted)
        if rows:
            self.db.queue_assets(rows)


def _load_assets(scene: Scene) -> dict[str, Any]:
    raw = scene.row.get("assets_json") or "{}"
    try:
        loaded = json.loads(raw)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _fmt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "open"


class _Stopped(Exception):
    """Internal signal for a user-requested stop."""
