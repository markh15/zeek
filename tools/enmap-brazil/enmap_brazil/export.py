"""Export the catalogue to formats other tools speak.

The SQLite file is already the queryable store; these exports exist for handing
a slice to GIS software, a notebook, or a columnar engine.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from .db import Database

FORMATS = ("csv", "geojson", "ndjson", "parquet")

_EXPORT_COLUMNS = [
    "item_id", "collection", "datetime", "year", "month", "cloud_cover",
    "sun_elevation", "sun_azimuth", "platform", "instruments", "product_type",
    "processing_level", "gsd", "tile_id", "minx", "miny", "maxx", "maxy",
    "centroid_lon", "centroid_lat", "self_href",
]


def export(
    db: Database,
    destination: str | Path,
    fmt: str = "csv",
    *,
    sql: str | None = None,
    limit: int | None = None,
) -> tuple[Path, int]:
    """Write scenes to ``destination``. Returns ``(path, row_count)``."""
    fmt = fmt.lower()
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; pick one of {', '.join(FORMATS)}")
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    query = sql or (
        "SELECT " + ", ".join(_EXPORT_COLUMNS) + ", geometry_json FROM scenes ORDER BY datetime"
    )
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = db.conn.execute(query).fetchall()

    if fmt == "csv":
        return (path, _write_csv(path, rows))
    if fmt == "ndjson":
        return (path, _write_ndjson(path, rows))
    if fmt == "geojson":
        return (path, _write_geojson(path, rows))
    return (path, _write_parquet(path, rows))


def _columns(rows: Sequence[sqlite3.Row]) -> list[str]:
    return list(rows[0].keys()) if rows else _EXPORT_COLUMNS


def _write_csv(path: Path, rows: Sequence[sqlite3.Row]) -> int:
    columns = [c for c in _columns(rows) if c != "geometry_json"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[c] for c in columns])
    return len(rows)


def _write_ndjson(path: Path, rows: Sequence[sqlite3.Row]) -> int:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_row_dict(row), default=str) + "\n")
    return len(rows)


def _write_geojson(path: Path, rows: Sequence[sqlite3.Row]) -> int:
    features = []
    for row in rows:
        record = _row_dict(row)
        geometry = record.pop("geometry_json", None)
        if isinstance(geometry, str):
            try:
                geometry = json.loads(geometry)
            except ValueError:
                geometry = None
        if geometry is None and record.get("minx") is not None:
            geometry = _bbox_polygon(
                record["minx"], record["miny"], record["maxx"], record["maxy"]
            )
        features.append(
            {
                "type": "Feature",
                "id": record.get("item_id"),
                "geometry": geometry,
                "properties": record,
            }
        )
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle, default=str)
    return len(features)


def _write_parquet(path: Path, rows: Sequence[sqlite3.Row]) -> int:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "parquet export needs pyarrow: pip install 'enmap-brazil[parquet]'"
        ) from exc
    columns = _columns(rows)
    table = pa.table({c: [row[c] for row in rows] for c in columns})
    pq.write_table(table, path, compression="zstd")
    return len(rows)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _bbox_polygon(minx: float, miny: float, maxx: float, maxy: float) -> dict[str, Any]:
    return {
        "type": "Polygon",
        "coordinates": [
            [[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]
        ],
    }


def rows_to_text(
    headers: Sequence[str], rows: Iterable[Sequence[Any]], max_width: int = 32
) -> str:
    """Render a result set as an aligned fixed-width table for the terminal."""
    materialised = [list(row) for row in rows]
    if not headers:
        return ""

    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text if len(text) <= max_width else text[: max_width - 1] + "\u2026"

    body = [[cell(v) for v in row] for row in materialised]
    widths = [len(cell(h)) for h in headers]
    for row in body:
        for i, value in enumerate(row[: len(widths)]):
            widths[i] = max(widths[i], len(value))

    lines = [" | ".join(cell(h).ljust(w) for h, w in zip(headers, widths))]
    lines.append("-+-".join("-" * w for w in widths))
    for row in body:
        lines.append(" | ".join(v.ljust(w) for v, w in zip(row, widths)))
    return "\n".join(lines)
