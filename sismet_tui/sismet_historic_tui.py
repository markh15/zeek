#!/usr/bin/env python3
"""Terminal UI for exporting SISMET (Cooxupe) historic weather data to CSV.

    ./sismet_historic_tui.py                 # interactive TUI
    ./sismet_historic_tui.py --help          # non-interactive/batch options

SISMET only answers requests from Brazilian IP space, so every request is sent
through a SOCKS5 proxy by default (socks5h://127.0.0.1:1080 -- hostnames are
resolved at the proxy).  Turn it off with --no-proxy if you are already inside
Brazil.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import os
import queue
import sys
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sismet_client import (  # noqa: E402
    BASE_URL,
    DATASETS,
    DateParams,
    SismetClient,
    Station,
)
from sismet_download import DownloadJob, Progress, run_download  # noqa: E402
from sismet_net import HttpClient, NetworkError, ProxyConfig, check_proxy  # noqa: E402

CONFIG_PATH = os.path.expanduser("~/.config/sismet-tui/config.json")
MAX_LOG_LINES = 500


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class Settings:
    dataset: str = "estacao"
    station_codes: list = field(default_factory=list)
    start: str = ""
    end: str = ""
    window_days: int = 31
    output: str = "sismet_historic.csv"
    proxy_enabled: bool = True
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 1080
    verify_tls: bool = True
    dedupe: bool = True
    resume: bool = True
    date_start_param: str = ""
    date_end_param: str = ""
    date_format: str = ""
    base_url: str = BASE_URL

    def __post_init__(self):
        if not self.start:
            self.start = (dt.date.today() - dt.timedelta(days=365)).isoformat()
        if not self.end:
            self.end = dt.date.today().isoformat()

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> str:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(asdict(self), handle, indent=2)
        return CONFIG_PATH

    # -- derived objects ---------------------------------------------------
    def proxy(self) -> ProxyConfig:
        return ProxyConfig(
            enabled=self.proxy_enabled, host=self.proxy_host, port=self.proxy_port
        )

    def date_params(self) -> Optional[DateParams]:
        if self.date_start_param and self.date_end_param and self.date_format:
            return DateParams(self.date_start_param, self.date_end_param, self.date_format)
        return None

    def set_date_params(self, params: Optional[DateParams]) -> None:
        if params is None:
            self.date_start_param = self.date_end_param = self.date_format = ""
        else:
            self.date_start_param = params.start_name
            self.date_end_param = params.end_name
            self.date_format = params.date_format

    def start_date(self) -> dt.date:
        return dt.date.fromisoformat(self.start)

    def end_date(self) -> dt.date:
        return dt.date.fromisoformat(self.end)

    def validate(self) -> list:
        problems = []
        if self.dataset not in DATASETS:
            problems.append(f"unknown dataset {self.dataset!r}")
        if not self.station_codes:
            problems.append("no station selected (press F2, or edit the field by hand)")
        for label, value in (("start", self.start), ("end", self.end)):
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                problems.append(f"{label} date {value!r} is not YYYY-MM-DD")
        if not problems and self.start_date() > self.end_date():
            problems.append("start date is after end date")
        if self.window_days < 1:
            problems.append("window size must be at least 1 day")
        if not self.output.strip():
            problems.append("output path is empty")
        return problems


def build_client(settings: Settings, log: Callable[[str], None]) -> SismetClient:
    http = HttpClient(proxy=settings.proxy(), verify_tls=settings.verify_tls)
    http.log = log
    return SismetClient(http=http, base_url=settings.base_url or BASE_URL, log=log)


# --------------------------------------------------------------------------
# TUI
# --------------------------------------------------------------------------


class Tui:
    def __init__(self, stdscr, settings: Settings):
        self.stdscr = stdscr
        self.settings = settings
        self.stations: list[Station] = []
        self.log_lines: list[str] = []
        self.progress = Progress()
        self.status = "Ready. F2 loads stations, F3 probes date parameters, F5 starts."
        self.selected = 0
        self.worker: Optional[threading.Thread] = None
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.fields = [
            ("Dataset", self._render_dataset, self._edit_dataset),
            ("Station(s)", self._render_stations, self._edit_stations),
            ("Start date", lambda: self.settings.start, self._edit_start),
            ("End date", lambda: self.settings.end, self._edit_end),
            ("Window (days)", lambda: str(self.settings.window_days), self._edit_window),
            ("Output CSV", lambda: self.settings.output, self._edit_output),
            ("SOCKS5 proxy", self._render_proxy, self._edit_proxy),
            ("Verify TLS", self._render_tls, self._edit_tls),
            ("Date params", self._render_date_params, self._edit_date_params),
            ("Resume/dedupe", self._render_flags, self._edit_flags),
            ("Base URL", lambda: self.settings.base_url, self._edit_base_url),
        ]

    # -- logging (thread-safe via the event queue) -------------------------
    def log(self, message: str) -> None:
        self.events.put(("log", message))

    def _append_log(self, message: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        for line in str(message).splitlines() or [""]:
            self.log_lines.append(f"{stamp}  {line}")
        del self.log_lines[:-MAX_LOG_LINES]

    # -- field renderers ---------------------------------------------------
    def _render_dataset(self) -> str:
        return DATASETS[self.settings.dataset].label

    def _render_stations(self) -> str:
        codes = self.settings.station_codes
        if not codes:
            return "(none selected)"
        names = {s.code: s.name for s in self.stations}
        shown = ", ".join(f"{c}{' ' + names[c] if names.get(c) else ''}" for c in codes[:4])
        if len(codes) > 4:
            shown += f", +{len(codes) - 4} more"
        return f"{len(codes)} selected: {shown}"

    def _render_proxy(self) -> str:
        return self.settings.proxy().describe()

    def _render_tls(self) -> str:
        return "yes" if self.settings.verify_tls else "NO - insecure, certificates unchecked"

    def _render_date_params(self) -> str:
        params = self.settings.date_params()
        return params.describe() if params else "not set - press F3 to probe the server"

    def _render_flags(self) -> str:
        return (
            f"resume {'on' if self.settings.resume else 'off'}, "
            f"dedupe {'on' if self.settings.dedupe else 'off'}"
        )

    # -- field editors -----------------------------------------------------
    def _edit_dataset(self) -> None:
        keys = list(DATASETS)
        index = keys.index(self.settings.dataset)
        self.settings.dataset = keys[(index + 1) % len(keys)]
        self.stations = []
        self.settings.station_codes = []
        self.status = f"Dataset set to {DATASETS[self.settings.dataset].label}"

    def _edit_stations(self) -> None:
        if self.stations:
            self._station_picker()
        else:
            current = ", ".join(self.settings.station_codes)
            value = self._prompt("Station codes (comma separated), or F2 to load list:", current)
            if value is not None:
                codes = [c.strip() for c in value.replace(";", ",").split(",") if c.strip()]
                self.settings.station_codes = codes
                self.status = f"{len(codes)} station code(s) set"

    def _edit_start(self) -> None:
        value = self._prompt("Start date (YYYY-MM-DD):", self.settings.start)
        if value is not None:
            self.settings.start = value.strip()

    def _edit_end(self) -> None:
        value = self._prompt("End date (YYYY-MM-DD):", self.settings.end)
        if value is not None:
            self.settings.end = value.strip()

    def _edit_window(self) -> None:
        value = self._prompt("Window size in days (e.g. 31):", str(self.settings.window_days))
        if value is not None:
            try:
                self.settings.window_days = max(1, int(value.strip()))
            except ValueError:
                self.status = "Window size must be a whole number"

    def _edit_output(self) -> None:
        value = self._prompt("Output CSV path:", self.settings.output)
        if value is not None and value.strip():
            self.settings.output = os.path.expanduser(value.strip())

    def _edit_proxy(self) -> None:
        value = self._prompt(
            "SOCKS5 proxy as host:port (empty to disable):",
            f"{self.settings.proxy_host}:{self.settings.proxy_port}"
            if self.settings.proxy_enabled
            else "",
        )
        if value is None:
            return
        value = value.strip()
        if not value:
            self.settings.proxy_enabled = False
            self.status = "Proxy disabled - requests will go out directly"
            return
        host, _, port = value.rpartition(":")
        if not host:
            host, port = value, "1080"
        try:
            self.settings.proxy_port = int(port)
        except ValueError:
            self.status = "Port must be a number"
            return
        self.settings.proxy_host = host
        self.settings.proxy_enabled = True
        self.status = f"Proxy set to {self.settings.proxy().describe()} (F4 tests it)"

    def _edit_tls(self) -> None:
        self.settings.verify_tls = not self.settings.verify_tls
        if not self.settings.verify_tls:
            self.status = "TLS verification OFF - only do this if you know the cert is broken"
        else:
            self.status = "TLS verification on"

    def _edit_date_params(self) -> None:
        current = self.settings.date_params()
        value = self._prompt(
            "Date params as start,end,format (empty to clear):",
            f"{current.start_name},{current.end_name},{current.date_format}" if current else "",
        )
        if value is None:
            return
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 3 and all(parts):
            self.settings.set_date_params(DateParams(parts[0], parts[1], parts[2]))
            self.status = "Date parameters set by hand"
        elif not value.strip():
            self.settings.set_date_params(None)
            self.status = "Date parameters cleared"
        else:
            self.status = "Expected three comma-separated values"

    def _edit_base_url(self) -> None:
        value = self._prompt("SISMET base URL:", self.settings.base_url)
        if value is not None and value.strip():
            self.settings.base_url = value.strip().rstrip("/")

    def _edit_flags(self) -> None:
        if self.settings.resume and self.settings.dedupe:
            self.settings.dedupe = False
        elif self.settings.resume:
            self.settings.resume = False
            self.settings.dedupe = True
        else:
            self.settings.resume = True
            self.settings.dedupe = True
        self.status = self._render_flags()

    # -- background actions ------------------------------------------------
    def _busy(self) -> bool:
        if self.worker and self.worker.is_alive():
            self.status = "Busy - press ESC to cancel the running task first"
            return True
        return False

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        self.cancel.clear()
        self.worker = threading.Thread(target=target, name=name, daemon=True)
        self.worker.start()

    def action_test_proxy(self) -> None:
        if self._busy():
            return

        def work():
            try:
                self.log(check_proxy(self.settings.proxy()))
            except Exception as exc:
                self.log(f"Proxy test failed: {exc}")

        self.status = "Testing the proxy..."
        self._spawn(work, "proxy-test")

    def action_load_stations(self) -> None:
        if self._busy():
            return

        def work():
            client = build_client(self.settings, self.log)
            dataset = DATASETS[self.settings.dataset]
            self.log(f"Loading station list from {dataset.index_path} ...")
            try:
                stations = client.list_stations(dataset)
            except Exception as exc:
                self.log(f"Could not load stations: {exc}")
                self.events.put(("status", "Station list failed - enter codes by hand"))
                return
            self.events.put(("stations", stations))
            self.log(f"Loaded {len(stations)} entries.")

        self.status = "Loading stations..."
        self._spawn(work, "stations")

    def action_probe(self) -> None:
        if self._busy():
            return
        if not self.settings.station_codes:
            self.status = "Pick a station first - the probe needs one to query"
            return

        def work():
            client = build_client(self.settings, self.log)
            dataset = DATASETS[self.settings.dataset]
            end = dt.date.today() - dt.timedelta(days=1)
            start = end - dt.timedelta(days=6)
            try:
                params = client.probe_date_parameters(
                    dataset, self.settings.station_codes[0], start, end
                )
            except Exception as exc:
                self.log(f"Probe failed: {exc}")
                return
            self.events.put(("date_params", params))

        self.status = "Probing date parameters (this makes several requests)..."
        self._spawn(work, "probe")

    def action_start(self) -> None:
        if self._busy():
            return
        problems = self.settings.validate()
        if problems:
            self.status = "Fix: " + "; ".join(problems)
            return
        if self.settings.date_params() is None:
            self.status = "Date parameters unknown - press F3 to probe (or set them by hand)"
            return

        selected = {code: Station(code, "") for code in self.settings.station_codes}
        for station in self.stations:
            if station.code in selected:
                selected[station.code] = station

        job = DownloadJob(
            dataset=DATASETS[self.settings.dataset],
            stations=list(selected.values()),
            start=self.settings.start_date(),
            end=self.settings.end_date(),
            output=self.settings.output,
            window_days=self.settings.window_days,
            date_params=self.settings.date_params(),
            dedupe=self.settings.dedupe,
            resume=self.settings.resume,
        )

        def work():
            client = build_client(self.settings, self.log)
            try:
                run_download(
                    client,
                    job,
                    lambda p: self.events.put(("progress", p)),
                    self.log,
                    self.cancel,
                )
            except Exception as exc:
                self.log(f"Download aborted: {exc}")
                self.events.put(("status", f"Aborted: {exc}"))

        self.progress = Progress()
        self.status = f"Downloading into {job.output} ... (ESC cancels)"
        self._spawn(work, "download")

    # -- event pump --------------------------------------------------------
    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "log":
                self._append_log(payload)
            elif kind == "status":
                self.status = payload
            elif kind == "stations":
                self.stations = payload
                self.status = f"{len(payload)} stations loaded - press ENTER to pick"
            elif kind == "date_params":
                self.settings.set_date_params(payload)
                self.status = (
                    f"Date parameters discovered: {payload.describe()}"
                    if payload
                    else "No candidate worked - set them by hand on the Date params row"
                )
            elif kind == "progress":
                self.progress = payload
                if payload.finished:
                    self.status = (
                        f"Done: {payload.rows} rows, {payload.failures} failed window(s)"
                    )

    # -- drawing -----------------------------------------------------------
    def _put(self, row: int, col: int, text: str, attr: int = curses.A_NORMAL) -> None:
        """Write one line, clipped to the window.

        curses refuses a write that fills the final cell of the last line (the
        cursor would have nowhere to advance to), so the last column is always
        left alone rather than special-casing the footer.
        """
        height, width = self.stdscr.getmaxyx()
        if not (0 <= row < height) or col >= width - 1:
            return
        try:
            self.stdscr.addnstr(row, col, text, max(0, width - 1 - col), attr)
        except curses.error:
            pass

    def _draw(self) -> None:
        stdscr = self.stdscr
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 16 or width < 60:
            self._put(0, 0, "Terminal too small - needs at least 60x16")
            stdscr.refresh()
            return

        title = " SISMET historic export - Cooxupe weather stations "
        self._put(0, 0, title.ljust(width), curses.A_REVERSE)

        row = 2
        label_width = max(len(name) for name, _, _ in self.fields) + 2
        for index, (name, render, _) in enumerate(self.fields):
            if row >= height - 1:
                break
            marker = ">" if index == self.selected else " "
            try:
                value = render()
            except Exception as exc:  # a broken renderer must not kill the UI
                value = f"<{exc}>"
            line = f"{marker} {name.ljust(label_width)}: {value}"
            attr = curses.A_BOLD if index == self.selected else curses.A_NORMAL
            if index == self.selected:
                attr |= self._colour(curses.COLOR_CYAN)
            self._put(row, 0, line, attr)
            row += 1

        row += 1
        if row < height - 1:
            row = self._draw_progress(row, width, height)

        if row < height - 2:
            self._put(row, 0, "Log:", curses.A_BOLD)
            row += 1
            available = height - 2 - row
            for line in self.log_lines[-available:] if available > 0 else []:
                self._put(row, 0, line)
                row += 1

        self._put(height - 2, 0, self.status.ljust(width), self._colour(curses.COLOR_YELLOW))
        keys = (
            " UP/DN move  ENTER edit  F2 stations  F3 probe  F4 test proxy  "
            "F5 start  ESC cancel  ^S save  q quit "
        )
        self._put(height - 1, 0, keys.ljust(width), curses.A_REVERSE)
        stdscr.refresh()

    def _draw_progress(self, row: int, width: int, height: int) -> int:
        progress = self.progress
        if not progress.total:
            return row
        bar_width = max(10, min(width - 30, 50))
        filled = int(bar_width * progress.done / progress.total) if progress.total else 0
        bar = "#" * filled + "-" * (bar_width - filled)
        summary = (
            f"[{bar}] {progress.percent:5.1f}%  "
            f"{progress.done}/{progress.total} windows  {progress.rows} rows"
        )
        if progress.failures:
            summary += f"  ({progress.failures} failed)"
        self._put(row, 0, summary, curses.A_BOLD)
        row += 1
        if progress.label and row < height - 1:
            self._put(row, 0, f"  {progress.label}")
            row += 1
        return row

    def _colour(self, colour: int) -> int:
        if not curses.has_colors():
            return curses.A_NORMAL
        pair = {curses.COLOR_CYAN: 1, curses.COLOR_YELLOW: 2}.get(colour, 0)
        return curses.color_pair(pair) if pair else curses.A_NORMAL

    # -- modal helpers -----------------------------------------------------
    def _prompt(self, label: str, initial: str = "") -> Optional[str]:
        """Single-line editor on the status row. ESC cancels, ENTER accepts."""
        buffer = list(initial)
        height, width = self.stdscr.getmaxyx()
        curses.curs_set(1)
        try:
            while True:
                text = "".join(buffer)
                shown = f"{label} {text}"
                if len(shown) > width - 2:
                    shown = shown[-(width - 2):]
                self._put(height - 2, 0, shown.ljust(width), self._colour(curses.COLOR_YELLOW))
                try:
                    self.stdscr.move(height - 2, min(len(shown), width - 2))
                except curses.error:
                    pass
                self.stdscr.refresh()
                key = self.stdscr.getch()
                if key in (curses.KEY_ENTER, 10, 13):
                    return "".join(buffer)
                if key == 27:  # ESC
                    return None
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    if buffer:
                        buffer.pop()
                elif key == 21:  # ^U clears
                    buffer.clear()
                elif key == curses.KEY_RESIZE:
                    height, width = self.stdscr.getmaxyx()
                elif 32 <= key < 127:
                    buffer.append(chr(key))
        finally:
            curses.curs_set(0)

    def _station_picker(self) -> None:
        """Modal multi-select over the loaded station list."""
        selected = set(self.settings.station_codes)
        filter_text = ""
        cursor = 0
        top = 0
        while True:
            visible = [
                s
                for s in self.stations
                if not filter_text
                or filter_text.lower() in s.code.lower()
                or filter_text.lower() in s.name.lower()
            ]
            cursor = max(0, min(cursor, len(visible) - 1)) if visible else 0
            height, width = self.stdscr.getmaxyx()
            body = height - 4
            if cursor < top:
                top = cursor
            elif cursor >= top + body:
                top = cursor - body + 1

            self.stdscr.erase()
            header = f" Stations - {len(selected)} selected "
            if filter_text:
                header += f"| filter: {filter_text} "
            self._put(0, 0, header.ljust(width), curses.A_REVERSE)

            for offset, station in enumerate(visible[top: top + body]):
                index = top + offset
                mark = "[x]" if station.code in selected else "[ ]"
                line = f"{mark} {station.code:>8}  {station.name}"
                attr = curses.A_REVERSE if index == cursor else curses.A_NORMAL
                self._put(1 + offset, 0, line, attr)

            keys = " SPACE toggle  / filter  a all  n none  ENTER confirm  ESC cancel "
            self._put(height - 1, 0, keys.ljust(width), curses.A_REVERSE)
            self.stdscr.refresh()

            key = self.stdscr.getch()
            if key in (curses.KEY_ENTER, 10, 13):
                ordered = [s.code for s in self.stations if s.code in selected]
                self.settings.station_codes = ordered
                self.status = f"{len(ordered)} station(s) selected"
                return
            if key == 27:
                return
            if key == curses.KEY_UP:
                cursor -= 1
            elif key == curses.KEY_DOWN:
                cursor += 1
            elif key == curses.KEY_NPAGE:
                cursor += body
            elif key == curses.KEY_PPAGE:
                cursor -= body
            elif key == ord(" ") and visible:
                code = visible[cursor].code
                selected.symmetric_difference_update({code})
            elif key == ord("a"):
                selected.update(s.code for s in visible)
            elif key == ord("n"):
                selected.difference_update(s.code for s in visible)
            elif key == ord("/"):
                entered = self._prompt("Filter:", filter_text)
                filter_text = entered or ""
                cursor = top = 0
            cursor = max(0, cursor)

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.timeout(120)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)

        self._append_log("SISMET historic export ready.")
        self._append_log(f"Proxy: {self.settings.proxy().describe()}")

        while True:
            self._drain_events()
            self._draw()
            key = self.stdscr.getch()
            if key == -1:
                continue
            if key in (curses.KEY_UP, ord("k")):
                self.selected = (self.selected - 1) % len(self.fields)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = (self.selected + 1) % len(self.fields)
            elif key in (curses.KEY_ENTER, 10, 13):
                self.stdscr.nodelay(False)
                try:
                    self.fields[self.selected][2]()
                finally:
                    self.stdscr.nodelay(True)
                    self.stdscr.timeout(120)
            elif key == curses.KEY_F2:
                self.action_load_stations()
            elif key == curses.KEY_F3:
                self.action_probe()
            elif key == curses.KEY_F4:
                self.action_test_proxy()
            elif key == curses.KEY_F5:
                self.action_start()
            elif key == 27:  # ESC
                if self.worker and self.worker.is_alive():
                    self.cancel.set()
                    self.status = "Cancelling after the current request..."
            elif key == 19:  # ^S
                self.status = f"Settings saved to {self.settings.save()}"
            elif key in (ord("q"), ord("Q")):
                if self.worker and self.worker.is_alive():
                    self.cancel.set()
                    self.worker.join(timeout=5)
                return
            elif key == curses.KEY_RESIZE:
                curses.update_lines_cols()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download SISMET (Cooxupe) historic weather data to CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dataset", choices=sorted(DATASETS), default=None,
                        help="which SISMET dataset to export")
    parser.add_argument("--stations", default=None,
                        help="comma-separated station codes")
    parser.add_argument("--start", default=None, help="first day, YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="last day, YYYY-MM-DD")
    parser.add_argument("--window-days", type=int, default=None,
                        help="days per request")
    parser.add_argument("-o", "--output", default=None, help="output CSV path")
    parser.add_argument("--proxy", default=None,
                        help="SOCKS5 proxy host:port (default 127.0.0.1:1080)")
    parser.add_argument("--no-proxy", action="store_true",
                        help="connect directly, without the SOCKS5 tunnel")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification")
    parser.add_argument("--date-params", default=None,
                        help="date parameters as start,end,strftime-format")
    parser.add_argument("--base-url", default=None, help="override the SISMET base URL")
    parser.add_argument("--list-stations", action="store_true",
                        help="print the station list and exit")
    parser.add_argument("--probe", action="store_true",
                        help="probe date-parameter names and exit")
    parser.add_argument("--batch", action="store_true",
                        help="run the download without the TUI")
    return parser.parse_args(argv)


def apply_args(settings: Settings, args: argparse.Namespace) -> None:
    if args.dataset:
        settings.dataset = args.dataset
    if args.stations:
        settings.station_codes = [
            c.strip() for c in args.stations.replace(";", ",").split(",") if c.strip()
        ]
    if args.start:
        settings.start = args.start
    if args.end:
        settings.end = args.end
    if args.window_days:
        settings.window_days = args.window_days
    if args.output:
        settings.output = os.path.expanduser(args.output)
    if args.no_proxy:
        settings.proxy_enabled = False
    elif args.proxy:
        host, _, port = args.proxy.rpartition(":")
        settings.proxy_host = host or args.proxy
        settings.proxy_enabled = True
        try:
            settings.proxy_port = int(port)
        except ValueError:
            settings.proxy_port = 1080
    if args.base_url:
        settings.base_url = args.base_url
    if args.insecure:
        settings.verify_tls = False
    if args.date_params:
        parts = [p.strip() for p in args.date_params.split(",")]
        if len(parts) != 3 or not all(parts):
            raise SystemExit("--date-params expects start,end,format")
        settings.set_date_params(DateParams(*parts))


def run_batch(settings: Settings, args: argparse.Namespace) -> int:
    def log(message: str) -> None:
        print(message, flush=True)

    client = build_client(settings, log)
    dataset = DATASETS[settings.dataset]

    if args.list_stations:
        for station in client.list_stations(dataset):
            print(f"{station.code}\t{station.name}")
        return 0

    if args.probe:
        if not settings.station_codes:
            raise SystemExit("--probe needs --stations")
        end = dt.date.today() - dt.timedelta(days=1)
        params = client.probe_date_parameters(
            dataset, settings.station_codes[0], end - dt.timedelta(days=6), end
        )
        if params is None:
            return 1
        print(f"\nUse: --date-params {params.start_name},{params.end_name},{params.date_format}")
        return 0

    problems = settings.validate()
    if problems:
        raise SystemExit("Cannot start: " + "; ".join(problems))
    if settings.date_params() is None:
        raise SystemExit(
            "Date parameters unknown. Run with --probe first, then pass --date-params."
        )

    job = DownloadJob(
        dataset=dataset,
        stations=[Station(code, "") for code in settings.station_codes],
        start=settings.start_date(),
        end=settings.end_date(),
        output=settings.output,
        window_days=settings.window_days,
        date_params=settings.date_params(),
        dedupe=settings.dedupe,
        resume=settings.resume,
    )
    progress = run_download(client, job, lambda p: None, log, threading.Event())
    return 1 if progress.failures else 0


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = Settings.load()
    apply_args(settings, args)

    if args.batch or args.list_stations or args.probe:
        try:
            return run_batch(settings, args)
        except NetworkError as exc:
            print(f"Network error: {exc}", file=sys.stderr)
            return 2

    if not sys.stdout.isatty():
        print(
            "Not a terminal - use --batch (with --stations/--date-params) for scripted runs.",
            file=sys.stderr,
        )
        return 2

    curses.wrapper(lambda stdscr: Tui(stdscr, settings).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
