"""Download orchestration: chunked, resumable, cancellable historic export."""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from sismet_client import (
    CsvSink,
    Dataset,
    DateParams,
    SismetClient,
    Station,
    iter_windows,
)
from sismet_net import NetworkError


@dataclass
class DownloadJob:
    dataset: Dataset
    stations: Sequence[Station]
    start: dt.date
    end: dt.date
    output: str
    window_days: int = 31
    date_params: Optional[DateParams] = None
    extra_params: dict = field(default_factory=dict)
    dedupe: bool = True
    resume: bool = True
    pause_seconds: float = 0.4  # be a polite neighbour to a co-op's server

    def windows(self) -> list[tuple[Station, dt.date, dt.date]]:
        units: list[tuple[Station, dt.date, dt.date]] = []
        for station in self.stations:
            for win_start, win_end in iter_windows(self.start, self.end, self.window_days):
                units.append((station, win_start, win_end))
        return units


@dataclass
class Progress:
    total: int = 0
    done: int = 0
    rows: int = 0
    failures: int = 0
    label: str = ""
    finished: bool = False
    error: Optional[str] = None

    @property
    def percent(self) -> float:
        return (self.done / self.total * 100.0) if self.total else 0.0


class _StateFile:
    """Records completed windows so an interrupted export can resume."""

    def __init__(self, output: str, enabled: bool):
        self.path = output + ".state.json"
        self.enabled = enabled
        self.completed: set[str] = set()
        if enabled and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                self.completed = set(data.get("completed", []))
            except (OSError, json.JSONDecodeError, AttributeError):
                self.completed = set()

    @staticmethod
    def key(station: Station, start: dt.date, end: dt.date) -> str:
        return f"{station.code}|{start.isoformat()}|{end.isoformat()}"

    def mark(self, key: str) -> None:
        if not self.enabled:
            return
        self.completed.add(key)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"completed": sorted(self.completed)}, handle)
            os.replace(tmp, self.path)
        except OSError:
            pass  # a failed bookmark must never abort a working download

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass


def run_download(
    client: SismetClient,
    job: DownloadJob,
    on_progress: Callable[[Progress], None],
    log: Callable[[str], None],
    cancel: threading.Event,
) -> Progress:
    """Execute ``job``, streaming rows into the CSV as each window arrives."""
    units = job.windows()
    progress = Progress(total=len(units))
    state = _StateFile(job.output, job.resume)

    already = sum(1 for st, s, e in units if _StateFile.key(st, s, e) in state.completed)
    if already:
        log(f"Resuming: {already} of {len(units)} windows already done.")

    sink = CsvSink(job.output, append=job.resume and bool(state.completed), dedupe=job.dedupe)
    progress.rows = sink.rows_written
    try:
        for station, win_start, win_end in units:
            if cancel.is_set():
                log("Cancelled by user.")
                break

            key = _StateFile.key(station, win_start, win_end)
            label = f"{station.code} {win_start:%Y-%m-%d} -> {win_end:%Y-%m-%d}"
            if key in state.completed:
                progress.done += 1
                progress.label = f"{label} (cached)"
                on_progress(progress)
                continue

            progress.label = label
            on_progress(progress)
            try:
                rows = client.fetch_window(
                    job.dataset,
                    station.code,
                    win_start,
                    win_end,
                    job.date_params,
                    job.extra_params or None,
                )
            except NetworkError as exc:
                progress.failures += 1
                progress.done += 1
                log(f"FAILED {label}: {exc}")
                on_progress(progress)
                continue

            written = sink.write(rows)
            progress.rows = sink.rows_written
            progress.done += 1
            state.mark(key)
            if rows:
                log(f"{label}: {len(rows)} rows ({written} new)")
            else:
                log(f"{label}: no data")
            on_progress(progress)

            if job.pause_seconds and not cancel.is_set():
                time.sleep(job.pause_seconds)
    finally:
        sink.close()

    progress.finished = True
    if sink.duplicates_skipped:
        log(f"Skipped {sink.duplicates_skipped} duplicate rows.")
    if progress.done >= progress.total and not progress.failures and not cancel.is_set():
        state.clear()  # a clean run leaves no resume bookmark behind
    log(f"Wrote {progress.rows} rows to {job.output}")
    on_progress(progress)
    return progress
