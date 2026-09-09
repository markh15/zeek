"""Geographic constants and work-chunking helpers for Brazil-wide harvests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterator, Sequence

BBox = tuple[float, float, float, float]

#: Brazilian territory including the oceanic islands (Fernando de Noronha,
#: Atol das Rocas, Sao Pedro e Sao Paulo, Trindade e Martim Vaz).
BRAZIL_BBOX: BBox = (-73.99, -33.76, -28.84, 5.28)

#: Continental Brazil only; stops at Ponta do Seixas (PB), the easternmost
#: point of the mainland. Roughly 12 degrees narrower than BRAZIL_BBOX.
BRAZIL_MAINLAND_BBOX: BBox = (-73.99, -33.76, -34.79, 5.28)

REGIONS: dict[str, BBox] = {
    "brazil": BRAZIL_BBOX,
    "brazil-mainland": BRAZIL_MAINLAND_BBOX,
    "amazonia-legal": (-73.99, -18.05, -43.99, 5.28),
    "cerrado": (-60.0, -24.7, -41.0, -2.2),
    "caatinga": (-45.5, -17.4, -34.8, -2.7),
    "mata-atlantica": (-56.0, -33.8, -34.8, -3.8),
    "pantanal": (-59.0, -22.0, -54.5, -15.5),
    "pampa": (-58.0, -33.8, -49.5, -28.0),
}


def parse_bbox(text: str) -> BBox:
    """Parse ``minx,miny,maxx,maxy`` or a named region into a bbox."""
    key = text.strip().lower()
    if key in REGIONS:
        return REGIONS[key]
    parts = [p for p in text.replace(" ", "").split(",") if p]
    if len(parts) != 4:
        raise ValueError(
            f"bbox must be 'minx,miny,maxx,maxy' or one of {sorted(REGIONS)}: {text!r}"
        )
    try:
        minx, miny, maxx, maxy = (float(p) for p in parts)
    except ValueError as exc:  # pragma: no cover - message clarity only
        raise ValueError(f"bbox values must be numbers: {text!r}") from exc
    if minx >= maxx or miny >= maxy:
        raise ValueError(f"bbox must be ordered minx,miny,maxx,maxy: {text!r}")
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180):
        raise ValueError(f"bbox longitudes out of range: {text!r}")
    if not (-90 <= miny <= 90 and -90 <= maxy <= 90):
        raise ValueError(f"bbox latitudes out of range: {text!r}")
    return (minx, miny, maxx, maxy)


def bbox_intersects(a: BBox, b: BBox) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def geometry_bounds(geometry: dict | None) -> BBox | None:
    """Compute the bounding box of any GeoJSON geometry, ignoring holes."""
    if not geometry:
        return None
    coords = geometry.get("coordinates")
    if coords is None:
        if geometry.get("type") == "GeometryCollection":
            boxes = [
                b
                for g in geometry.get("geometries", [])
                if (b := geometry_bounds(g)) is not None
            ]
            if not boxes:
                return None
            return (
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            )
        return None

    xs: list[float] = []
    ys: list[float] = []

    def walk(node) -> None:
        if isinstance(node, (int, float)):
            return
        if (
            len(node) >= 2
            and isinstance(node[0], (int, float))
            and isinstance(node[1], (int, float))
        ):
            xs.append(float(node[0]))
            ys.append(float(node[1]))
            return
        for child in node:
            walk(child)

    walk(coords)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True)
class Chunk:
    """One resumable unit of work: a bbox crossed with a half-open time window."""

    bbox: BBox
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        return "{:.4f},{:.4f},{:.4f},{:.4f}|{}|{}".format(
            *self.bbox, self.start.strftime("%Y%m%dT%H%M%S"), self.end.strftime("%Y%m%dT%H%M%S")
        )

    @property
    def datetime_range(self) -> str:
        """RFC 3339 interval for the STAC ``datetime`` parameter.

        The window is half-open, so one second is shaved off the upper bound to
        keep adjacent windows from both claiming the same instant.
        """
        upper = self.end - timedelta(seconds=1)
        return f"{_rfc3339(self.start)}/{_rfc3339(upper)}"

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%d} → {self.end:%Y-%m-%d}"


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def add_months(value: datetime, months: int) -> datetime:
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    return value.replace(year=year, month=month, day=1)


def month_windows(
    start: date | datetime, end: date | datetime, months: int = 1
) -> Iterator[tuple[datetime, datetime]]:
    """Yield half-open ``[start, end)`` windows of ``months`` months each.

    Windows are snapped to month boundaries so that repeated runs with slightly
    different start dates reuse the same chunk keys (and therefore resume).
    """
    if months < 1:
        raise ValueError("months must be >= 1")
    lo = _as_utc(start)
    hi = _as_utc(end)
    if hi <= lo:
        return
    cursor = lo.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor < hi:
        nxt = add_months(cursor, months)
        yield (max(cursor, lo), min(nxt, hi))
        cursor = nxt


def grid_tiles(bbox: BBox, size_deg: float | None) -> list[BBox]:
    """Split ``bbox`` into a grid of at most ``size_deg``-wide tiles."""
    if not size_deg or size_deg <= 0:
        return [bbox]
    minx, miny, maxx, maxy = bbox
    nx = max(1, math.ceil((maxx - minx) / size_deg))
    ny = max(1, math.ceil((maxy - miny) / size_deg))
    tiles: list[BBox] = []
    for iy in range(ny):
        y0 = miny + iy * size_deg
        y1 = min(maxy, y0 + size_deg)
        for ix in range(nx):
            x0 = minx + ix * size_deg
            x1 = min(maxx, x0 + size_deg)
            tiles.append((round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)))
    return tiles


def build_chunks(
    bbox: BBox,
    start: date | datetime,
    end: date | datetime,
    window_months: int = 3,
    grid_deg: float | None = None,
) -> list[Chunk]:
    """Cross the spatial grid with the time windows to get the work list."""
    tiles = grid_tiles(bbox, grid_deg)
    chunks: list[Chunk] = []
    for lo, hi in month_windows(start, end, window_months):
        for tile in tiles:
            chunks.append(Chunk(bbox=tile, start=lo, end=hi))
    return chunks


def clamp_range(
    start: datetime,
    end: datetime,
    extent: tuple[datetime | None, datetime | None] | None,
) -> tuple[datetime, datetime] | None:
    """Intersect a requested range with a collection's temporal extent.

    Returns ``None`` when the two do not overlap, which is how a 10-year
    request against a mission that only launched in 2022 gets trimmed down to
    the years that actually exist.
    """
    if not extent:
        return (start, end)
    lo, hi = extent
    new_start = max(start, lo) if lo else start
    new_end = min(end, hi) if hi else end
    if new_end <= new_start:
        return None
    return (new_start, new_end)


def summarize_bboxes(boxes: Sequence[BBox]) -> BBox | None:
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
