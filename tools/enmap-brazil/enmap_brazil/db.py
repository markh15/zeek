"""SQLite storage for the harvested EnMAP catalogue.

Everything lands in one portable ``.sqlite`` file: scene metadata as typed
columns (so plain SQL works), the untouched STAC JSON alongside it (so nothing
is lost), and an R*Tree index (so spatial filtering over a decade of Brazil
stays fast). Any SQLite client -- the ``sqlite3`` shell, pandas, DuckDB, QGIS --
can read it without this package.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .regions import BBox, geometry_bounds
from .stac import parse_datetime

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS collections (
    id             TEXT PRIMARY KEY,
    title          TEXT,
    description    TEXT,
    license        TEXT,
    temporal_start TEXT,
    temporal_end   TEXT,
    bbox_json      TEXT,
    raw_json       TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenes (
    scene_key        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          TEXT NOT NULL,
    collection       TEXT NOT NULL,
    datetime         TEXT,
    start_datetime   TEXT,
    end_datetime     TEXT,
    year             INTEGER,
    month            INTEGER,
    minx             REAL,
    miny             REAL,
    maxx             REAL,
    maxy             REAL,
    centroid_lon     REAL,
    centroid_lat     REAL,
    cloud_cover      REAL,
    sun_elevation    REAL,
    sun_azimuth      REAL,
    platform         TEXT,
    instruments      TEXT,
    constellation    TEXT,
    product_type     TEXT,
    processing_level TEXT,
    gsd              REAL,
    tile_id          TEXT,
    self_href        TEXT,
    geometry_json    TEXT,
    assets_json      TEXT,
    links_json       TEXT,
    properties_json  TEXT NOT NULL,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    UNIQUE (collection, item_id)
);

CREATE INDEX IF NOT EXISTS idx_scenes_datetime    ON scenes (datetime);
CREATE INDEX IF NOT EXISTS idx_scenes_year_month  ON scenes (year, month);
CREATE INDEX IF NOT EXISTS idx_scenes_collection  ON scenes (collection, datetime);
CREATE INDEX IF NOT EXISTS idx_scenes_cloud       ON scenes (cloud_cover);
CREATE INDEX IF NOT EXISTS idx_scenes_item        ON scenes (item_id);

CREATE VIRTUAL TABLE IF NOT EXISTS scenes_bbox USING rtree (
    id, minx, maxx, miny, maxy
);

/* Keep the R*Tree in lockstep with scenes no matter who writes.
   The rtree module ignores OR REPLACE, so each trigger deletes first. */
CREATE TRIGGER scenes_bbox_ai AFTER INSERT ON scenes
WHEN NEW.minx IS NOT NULL
BEGIN
    DELETE FROM scenes_bbox WHERE id = NEW.scene_key;
    INSERT INTO scenes_bbox (id, minx, maxx, miny, maxy)
    VALUES (NEW.scene_key, NEW.minx, NEW.maxx, NEW.miny, NEW.maxy);
END;

CREATE TRIGGER scenes_bbox_au AFTER UPDATE ON scenes
BEGIN
    DELETE FROM scenes_bbox WHERE id = NEW.scene_key;
    INSERT INTO scenes_bbox (id, minx, maxx, miny, maxy)
    SELECT NEW.scene_key, NEW.minx, NEW.maxx, NEW.miny, NEW.maxy
    WHERE NEW.minx IS NOT NULL;
END;

CREATE TRIGGER scenes_bbox_ad AFTER DELETE ON scenes
BEGIN
    DELETE FROM scenes_bbox WHERE id = OLD.scene_key;
END;

CREATE TABLE IF NOT EXISTS harvest_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    collections TEXT,
    bbox        TEXT,
    dt_start    TEXT,
    dt_end      TEXT,
    params_json TEXT,
    items_seen  INTEGER NOT NULL DEFAULT 0,
    items_new   INTEGER NOT NULL DEFAULT 0,
    chunks_done INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS harvest_chunks (
    collection TEXT NOT NULL,
    chunk_key  TEXT NOT NULL,
    minx REAL, miny REAL, maxx REAL, maxy REAL,
    dt_start   TEXT,
    dt_end     TEXT,
    status     TEXT NOT NULL,
    items      INTEGER NOT NULL DEFAULT 0,
    pages      INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (collection, chunk_key)
);

CREATE INDEX IF NOT EXISTS idx_chunks_status ON harvest_chunks (status);

CREATE TABLE IF NOT EXISTS assets (
    asset_key   INTEGER PRIMARY KEY AUTOINCREMENT,
    collection  TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    href        TEXT NOT NULL,
    media_type  TEXT,
    title       TEXT,
    roles       TEXT,
    file_size   INTEGER,
    local_path  TEXT,
    sha256      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (collection, item_id, key)
);

CREATE INDEX IF NOT EXISTS idx_assets_status ON assets (status);

CREATE VIEW IF NOT EXISTS v_scenes AS
SELECT item_id, collection, datetime, year, month,
       cloud_cover, sun_elevation, platform, processing_level, product_type,
       tile_id, minx, miny, maxx, maxy, centroid_lon, centroid_lat, self_href
FROM scenes;

CREATE VIEW IF NOT EXISTS v_scenes_by_month AS
SELECT collection, year, month, COUNT(*) AS scenes,
       ROUND(AVG(cloud_cover), 2) AS avg_cloud_cover
FROM scenes
GROUP BY collection, year, month
ORDER BY year, month;

CREATE VIEW IF NOT EXISTS v_scenes_by_year AS
SELECT collection, year, COUNT(*) AS scenes,
       ROUND(AVG(cloud_cover), 2) AS avg_cloud_cover,
       MIN(datetime) AS first_scene, MAX(datetime) AS last_scene
FROM scenes
GROUP BY collection, year
ORDER BY year;
"""

#: Canned queries surfaced in the TUI's Browse tab and via ``enmap-br query``.
CANNED_QUERIES: dict[str, str] = {
    "recent": (
        "SELECT item_id, collection, datetime, ROUND(cloud_cover,1) AS cloud, "
        "ROUND(centroid_lon,3) AS lon, ROUND(centroid_lat,3) AS lat "
        "FROM scenes ORDER BY datetime DESC LIMIT 200"
    ),
    "by-year": "SELECT * FROM v_scenes_by_year",
    "by-month": "SELECT * FROM v_scenes_by_month",
    "clear-skies": (
        "SELECT item_id, datetime, ROUND(cloud_cover,1) AS cloud, "
        "ROUND(centroid_lon,3) AS lon, ROUND(centroid_lat,3) AS lat "
        "FROM scenes WHERE cloud_cover IS NOT NULL AND cloud_cover <= 10 "
        "ORDER BY datetime DESC LIMIT 200"
    ),
    "collections": (
        "SELECT c.id, c.temporal_start, c.temporal_end, "
        "(SELECT COUNT(*) FROM scenes s WHERE s.collection = c.id) AS scenes "
        "FROM collections c ORDER BY c.id"
    ),
    "amazon-clear": (
        "SELECT item_id, datetime, ROUND(cloud_cover,1) AS cloud, "
        "ROUND(centroid_lon,3) AS lon, ROUND(centroid_lat,3) AS lat FROM scenes "
        "WHERE centroid_lon BETWEEN -74 AND -44 AND centroid_lat BETWEEN -18 AND 5 "
        "AND (cloud_cover IS NULL OR cloud_cover <= 20) "
        "ORDER BY datetime DESC LIMIT 200"
    ),
    "chunk-progress": (
        "SELECT collection, status, COUNT(*) AS chunks, SUM(items) AS items "
        "FROM harvest_chunks GROUP BY collection, status ORDER BY collection, status"
    ),
    "download-queue": (
        "SELECT status, COUNT(*) AS assets, "
        "ROUND(SUM(COALESCE(file_size,0))/1048576.0, 1) AS mib "
        "FROM assets GROUP BY status"
    ),
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first(props: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in props and props[key] not in (None, "", []):
            return props[key]
    return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Scene:
    """A STAC item flattened into the columns we index on."""

    item_id: str
    collection: str
    row: dict[str, Any]

    @classmethod
    def from_item(cls, item: dict[str, Any], default_collection: str = "") -> "Scene":
        props = item.get("properties") or {}
        item_id = str(item.get("id") or props.get("id") or "")
        collection = str(item.get("collection") or default_collection or "unknown")

        bbox = item.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) >= 4):
            bbox = geometry_bounds(item.get("geometry"))
        if bbox and len(bbox) >= 6:  # 3D bbox: [minx,miny,minz,maxx,maxy,maxz]
            bbox = [bbox[0], bbox[1], bbox[3], bbox[4]]
        minx, miny, maxx, maxy = (
            (_as_float(bbox[0]), _as_float(bbox[1]), _as_float(bbox[2]), _as_float(bbox[3]))
            if bbox
            else (None, None, None, None)
        )

        dt = parse_datetime(
            _first(props, "datetime", "start_datetime", "acquisitionDate", "created")
        )
        instruments = _first(props, "instruments", "instrument")
        if isinstance(instruments, (list, tuple)):
            instruments = ",".join(str(i) for i in instruments)

        self_href = ""
        for link in item.get("links") or []:
            if isinstance(link, dict) and link.get("rel") == "self":
                self_href = str(link.get("href") or "")
                break

        row = {
            "item_id": item_id,
            "collection": collection,
            "datetime": dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None,
            "start_datetime": _stamp(props, "start_datetime"),
            "end_datetime": _stamp(props, "end_datetime"),
            "year": dt.year if dt else None,
            "month": dt.month if dt else None,
            "minx": minx,
            "miny": miny,
            "maxx": maxx,
            "maxy": maxy,
            "centroid_lon": (minx + maxx) / 2 if minx is not None and maxx is not None else None,
            "centroid_lat": (miny + maxy) / 2 if miny is not None and maxy is not None else None,
            "cloud_cover": _as_float(
                _first(props, "eo:cloud_cover", "cloud_cover", "cloudCover", "cloudCoverage")
            ),
            "sun_elevation": _as_float(
                _first(props, "view:sun_elevation", "sun_elevation", "sunElevation")
            ),
            "sun_azimuth": _as_float(
                _first(props, "view:sun_azimuth", "sun_azimuth", "sunAzimuth")
            ),
            "platform": _str_or_none(_first(props, "platform", "mission")),
            "instruments": _str_or_none(instruments),
            "constellation": _str_or_none(props.get("constellation")),
            "product_type": _str_or_none(
                _first(props, "product:type", "productType", "product_type")
            ),
            "processing_level": _str_or_none(
                _first(
                    props,
                    "processing:level",
                    "processingLevel",
                    "processing_level",
                    "product:processing_level",
                )
            ),
            "gsd": _as_float(_first(props, "gsd", "eo:gsd")),
            "tile_id": _str_or_none(
                _first(props, "enmap:tile_id", "tileId", "tile_id", "grid:code")
            ),
            "self_href": self_href,
            "geometry_json": json.dumps(item.get("geometry"), separators=(",", ":"))
            if item.get("geometry")
            else None,
            "assets_json": json.dumps(item.get("assets") or {}, separators=(",", ":")),
            "links_json": json.dumps(item.get("links") or [], separators=(",", ":")),
            "properties_json": json.dumps(props, separators=(",", ":"), default=str),
        }
        return cls(item_id=item_id, collection=collection, row=row)


def _stamp(props: dict[str, Any], key: str) -> str | None:
    value = parse_datetime(props.get(key))
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _str_or_none(value: Any) -> str | None:
    if value in (None, "", []):
        return None
    return str(value)


_SCENE_COLUMNS = [
    "item_id", "collection", "datetime", "start_datetime", "end_datetime",
    "year", "month", "minx", "miny", "maxx", "maxy", "centroid_lon",
    "centroid_lat", "cloud_cover", "sun_elevation", "sun_azimuth", "platform",
    "instruments", "constellation", "product_type", "processing_level", "gsd",
    "tile_id", "self_href", "geometry_json", "assets_json", "links_json",
    "properties_json",
]


class Database:
    """Thread-confined SQLite handle. Open one per thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._configure()
        self.migrate()

    def _configure(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    def migrate(self) -> None:
        with self._lock:
            # Recreate the triggers unconditionally so a fix to their bodies
            # reaches databases created by an older build.
            self.conn.executescript(
                "DROP TRIGGER IF EXISTS scenes_bbox_ai;"
                "DROP TRIGGER IF EXISTS scenes_bbox_au;"
                "DROP TRIGGER IF EXISTS scenes_bbox_ad;"
            )
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self.conn.cursor()
            try:
                yield cur
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()

    # -- meta --------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value) if not isinstance(value, str) else value),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # -- collections -------------------------------------------------------
    def upsert_collections(self, collections: Iterable[Any]) -> int:
        now = _utcnow()
        rows = [
            (
                c.id,
                c.title,
                c.description,
                c.license,
                c.temporal_start.strftime("%Y-%m-%dT%H:%M:%SZ") if c.temporal_start else None,
                c.temporal_end.strftime("%Y-%m-%dT%H:%M:%SZ") if c.temporal_end else None,
                json.dumps(c.bbox) if c.bbox else None,
                json.dumps(c.raw, separators=(",", ":"), default=str),
                now,
            )
            for c in collections
        ]
        if not rows:
            return 0
        with self.transaction() as cur:
            cur.executemany(
                "INSERT INTO collections (id, title, description, license, "
                "temporal_start, temporal_end, bbox_json, raw_json, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
                "description=excluded.description, license=excluded.license, "
                "temporal_start=excluded.temporal_start, temporal_end=excluded.temporal_end, "
                "bbox_json=excluded.bbox_json, raw_json=excluded.raw_json, "
                "updated_at=excluded.updated_at",
                rows,
            )
        return len(rows)

    def list_collections(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM collections ORDER BY id").fetchall()

    # -- scenes ------------------------------------------------------------
    def upsert_scenes(self, scenes: Sequence[Scene]) -> tuple[int, int]:
        """Insert or refresh scenes. Returns ``(seen, newly_inserted)``."""
        if not scenes:
            return (0, 0)
        now = _utcnow()
        placeholders = ",".join("?" for _ in _SCENE_COLUMNS)
        updates = ",".join(
            f"{col}=excluded.{col}" for col in _SCENE_COLUMNS if col not in ("item_id", "collection")
        )
        sql = (
            f"INSERT INTO scenes ({','.join(_SCENE_COLUMNS)}, first_seen, last_seen) "
            f"VALUES ({placeholders}, ?, ?) "
            f"ON CONFLICT(collection, item_id) DO UPDATE SET {updates}, last_seen=excluded.last_seen"
        )
        with self.transaction() as cur:
            before = cur.execute("SELECT COUNT(*) AS n FROM scenes").fetchone()["n"]
            cur.executemany(
                sql,
                [
                    tuple(s.row.get(col) for col in _SCENE_COLUMNS) + (now, now)
                    for s in scenes
                    if s.item_id
                ],
            )
            after = cur.execute("SELECT COUNT(*) AS n FROM scenes").fetchone()["n"]
        return (len(scenes), after - before)

    def scene_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM scenes").fetchone()["n"]

    def query_scenes(
        self,
        *,
        bbox: BBox | None = None,
        start: str | None = None,
        end: str | None = None,
        collection: str | None = None,
        max_cloud: float | None = None,
        limit: int = 500,
        order: str = "datetime DESC",
    ) -> list[sqlite3.Row]:
        """Typed convenience wrapper over the common filters (uses the R*Tree)."""
        sql = ["SELECT s.* FROM scenes s"]
        params: list[Any] = []
        where: list[str] = []
        if bbox:
            sql.append("JOIN scenes_bbox r ON r.id = s.scene_key")
            where.append("r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?")
            params += [bbox[0], bbox[2], bbox[1], bbox[3]]
        if start:
            where.append("s.datetime >= ?")
            params.append(start)
        if end:
            where.append("s.datetime <= ?")
            params.append(end)
        if collection:
            where.append("s.collection = ?")
            params.append(collection)
        if max_cloud is not None:
            where.append("(s.cloud_cover IS NULL OR s.cloud_cover <= ?)")
            params.append(max_cloud)
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append(f"ORDER BY s.{order}")
        sql.append("LIMIT ?")
        params.append(int(limit))
        return self.conn.execute(" ".join(sql), params).fetchall()

    def run_sql(self, sql: str, limit: int = 500) -> tuple[list[str], list[tuple]]:
        """Run a read-only statement, for the query pane. Rejects writes."""
        stripped = sql.strip().rstrip(";").strip()
        if not stripped:
            return ([], [])
        lowered = stripped.lower()
        if not (lowered.startswith("select") or lowered.startswith("with")):
            raise ValueError("only SELECT/WITH statements are allowed here")
        if ";" in stripped:
            raise ValueError("only a single statement is allowed")
        cur = self.conn.execute(stripped)
        rows = cur.fetchmany(limit)
        headers = [d[0] for d in cur.description or []]
        cur.close()
        return (headers, [tuple(r) for r in rows])

    # -- harvest bookkeeping ----------------------------------------------
    def start_run(self, **fields: Any) -> int:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO harvest_runs (started_at, status, collections, bbox, "
                "dt_start, dt_end, params_json) VALUES (?,?,?,?,?,?,?)",
                (
                    _utcnow(),
                    "running",
                    fields.get("collections"),
                    fields.get("bbox"),
                    fields.get("dt_start"),
                    fields.get("dt_end"),
                    json.dumps(fields.get("params") or {}, default=str),
                ),
            )
            return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, status: str, items_seen: int, items_new: int,
        chunks_done: int, error: str | None = None,
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE harvest_runs SET finished_at=?, status=?, items_seen=?, "
                "items_new=?, chunks_done=?, error=? WHERE run_id=?",
                (_utcnow(), status, items_seen, items_new, chunks_done, error, run_id),
            )

    def chunk_status(self, collection: str, chunk_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM harvest_chunks WHERE collection=? AND chunk_key=?",
            (collection, chunk_key),
        ).fetchone()
        return row["status"] if row else None

    def record_chunk(
        self, collection: str, chunk: Any, status: str,
        items: int = 0, pages: int = 0, error: str | None = None,
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO harvest_chunks (collection, chunk_key, minx, miny, maxx, "
                "maxy, dt_start, dt_end, status, items, pages, error, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(collection, chunk_key) DO UPDATE SET status=excluded.status, "
                "items=excluded.items, pages=excluded.pages, error=excluded.error, "
                "updated_at=excluded.updated_at",
                (
                    collection, chunk.key, chunk.bbox[0], chunk.bbox[1], chunk.bbox[2],
                    chunk.bbox[3], chunk.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    chunk.end.strftime("%Y-%m-%dT%H:%M:%SZ"), status, items, pages,
                    error, _utcnow(),
                ),
            )

    def reset_chunks(self, collection: str | None = None) -> int:
        """Forget chunk progress so the next harvest re-fetches everything."""
        with self.transaction() as cur:
            if collection:
                cur.execute("DELETE FROM harvest_chunks WHERE collection = ?", (collection,))
            else:
                cur.execute("DELETE FROM harvest_chunks")
            return cur.rowcount

    # -- assets ------------------------------------------------------------
    def queue_assets(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = _utcnow()
        with self.transaction() as cur:
            cur.executemany(
                "INSERT INTO assets (collection, item_id, key, href, media_type, "
                "title, roles, file_size, status, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'pending',?) "
                "ON CONFLICT(collection, item_id, key) DO UPDATE SET "
                "href=excluded.href, media_type=excluded.media_type, "
                "updated_at=excluded.updated_at",
                [
                    (
                        r["collection"], r["item_id"], r["key"], r["href"],
                        r.get("media_type"), r.get("title"),
                        json.dumps(r.get("roles")) if r.get("roles") else None,
                        r.get("file_size"), now,
                    )
                    for r in rows
                ],
            )
            return cur.rowcount

    def pending_assets(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM assets WHERE status IN ('pending','error') "
            "ORDER BY asset_key LIMIT ?",
            (limit,),
        ).fetchall()

    def update_asset(self, asset_key: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _utcnow()
        assignments = ",".join(f"{k}=?" for k in fields)
        with self.transaction() as cur:
            cur.execute(
                f"UPDATE assets SET {assignments} WHERE asset_key = ?",
                (*fields.values(), asset_key),
            )

    # -- reporting ---------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        cur = self.conn
        out: dict[str, Any] = {
            "scenes": self.scene_count(),
            "collections": cur.execute("SELECT COUNT(*) AS n FROM collections").fetchone()["n"],
            "db_path": str(self.path),
            "db_size_mb": round(self.path.stat().st_size / 1_048_576, 2)
            if self.path.exists()
            else 0.0,
        }
        row = cur.execute(
            "SELECT MIN(datetime) AS lo, MAX(datetime) AS hi FROM scenes"
        ).fetchone()
        out["first_scene"] = row["lo"]
        out["last_scene"] = row["hi"]
        out["chunks"] = {
            r["status"]: r["n"]
            for r in cur.execute(
                "SELECT status, COUNT(*) AS n FROM harvest_chunks GROUP BY status"
            ).fetchall()
        }
        out["assets"] = {
            r["status"]: r["n"]
            for r in cur.execute(
                "SELECT status, COUNT(*) AS n FROM assets GROUP BY status"
            ).fetchall()
        }
        out["per_collection"] = [
            dict(r)
            for r in cur.execute(
                "SELECT collection, COUNT(*) AS scenes, MIN(datetime) AS first_scene, "
                "MAX(datetime) AS last_scene FROM scenes GROUP BY collection ORDER BY collection"
            ).fetchall()
        ]
        return out
