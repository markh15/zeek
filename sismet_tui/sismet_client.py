"""SISMET (Cooxupe) historic data client.

SISMET is the meteorological system of Cooxupe -- Cooperativa Regional de
Cafeicultores em Guaxupe -- covering their network of automatic weather
stations and rain gauges across Minas Gerais and Sao Paulo.

Known endpoints (port 9000):

    /                                       landing page
    /dados/estacao/                         weather stations
    /dados/estacao/pesquisarDados/          historic station data   (?cdEstacao=N)
    /dados/pluviometros/                    rain gauges
    /dados/pluviometros/pesquisarDados/     historic gauge data     (?cdPto=N)
    /estacaoEmTempoReal/, /estacaoOnline/   real-time readings

The identifier parameters (``cdEstacao``, ``cdPto``) are confirmed.  The
date-range parameter names are *not* published anywhere, and the host only
answers from Brazilian IP space, so they cannot be checked from a CI box.
Rather than hard-coding a guess, :meth:`SismetClient.probe_date_parameters`
discovers them at runtime against the live server, and every candidate lives in
:data:`DATE_PARAM_CANDIDATES` where it is easy to correct.

Responses are handled format-agnostically -- JSON (bare list, wrapped, or
DRF-paginated), an HTML ``<table>``, or raw CSV all reduce to the same
``(fieldnames, rows)`` shape -- so the exporter works regardless of which one
the server actually returns.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterable, Iterator, Optional, Sequence

from sismet_net import HttpClient, NetworkError, Response

BASE_URL = "https://sismet.cooxupe.com.br:9000"

# Column appended to every CSV so that fields appearing only in later windows
# are never silently dropped from a single-pass streaming write.
EXTRA_COLUMN = "extra_json"


@dataclass(frozen=True)
class Dataset:
    key: str
    label: str
    index_path: str
    search_path: str
    id_param: str


DATASETS = {
    "estacao": Dataset(
        key="estacao",
        label="Estacoes meteorologicas (weather stations)",
        index_path="/dados/estacao/",
        search_path="/dados/estacao/pesquisarDados/",
        id_param="cdEstacao",
    ),
    "pluviometros": Dataset(
        key="pluviometros",
        label="Pluviometros (rain gauges)",
        index_path="/dados/pluviometros/",
        search_path="/dados/pluviometros/pesquisarDados/",
        id_param="cdPto",
    ),
}

# Ordered best-guess first; the probe walks this list until data comes back
# filtered.  Portuguese-language Django/Java systems overwhelmingly use one of
# these spellings.
DATE_PARAM_CANDIDATES: Sequence[tuple[str, str]] = (
    ("dtInicio", "dtFim"),
    ("dataInicial", "dataFinal"),
    ("dtInicial", "dtFinal"),
    ("dataInicio", "dataFim"),
    ("dtIni", "dtFim"),
    ("inicio", "fim"),
    ("data_inicio", "data_fim"),
    ("dataIni", "dataFim"),
    ("periodoInicial", "periodoFinal"),
    ("start", "end"),
    ("start_date", "end_date"),
)

DATE_FORMAT_CANDIDATES: Sequence[str] = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y")

# Keys that plausibly carry the observation timestamp, used for de-duplication
# and for validating that a date filter actually took effect.
TIMESTAMP_KEY_HINTS = (
    "data",
    "dataleitura",
    "datahora",
    "dthora",
    "dtleitura",
    "date",
    "datetime",
    "timestamp",
    "momento",
)


class SismetError(Exception):
    """A SISMET-level problem (bad response shape, no data, unknown dataset)."""


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


class _TableParser(HTMLParser):
    """Collect every <table> in a document as a list of rows of cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: Optional[list[list[str]]] = None
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = " ".join("".join(self._cell).split())
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table":
            if self._depth == 1 and self._table is not None:
                self.tables.append(self._table)
                self._table = None
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


class _OptionParser(HTMLParser):
    """Collect <option value=...>label</option> pairs from station pickers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.options: list[tuple[str, str]] = []
        self._value: Optional[str] = None
        self._label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "option":
            self._value = dict(attrs).get("value")
            self._label = []

    def handle_endtag(self, tag):
        if tag == "option" and self._value is not None:
            label = " ".join("".join(self._label).split())
            if self._value.strip():
                self.options.append((self._value.strip(), label))
            self._value = None
            self._label = []

    def handle_data(self, data):
        if self._value is not None:
            self._label.append(data)


def _flatten(record: dict, prefix: str = "") -> dict:
    """Flatten one level of nesting so nested objects still reach the CSV."""
    flat: dict = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, prefix=f"{name}."))
        elif isinstance(value, (list, tuple)):
            flat[name] = json.dumps(value, ensure_ascii=False)
        else:
            flat[name] = value
    return flat


def _rows_from_json(payload) -> list[dict]:
    """Reduce any plausible JSON envelope to a list of flat records."""
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("results", "dados", "data", "rows", "registros", "items", "content"):
            value = payload.get(key)
            if isinstance(value, list):
                records = value
                break
        else:
            # A single record, or a mapping of code -> record.
            values = list(payload.values())
            if values and all(isinstance(v, dict) for v in values):
                records = values
            else:
                records = [payload]
    else:
        return []

    rows: list[dict] = []
    for record in records:
        if isinstance(record, dict):
            rows.append(_flatten(record))
        elif isinstance(record, (list, tuple)):
            rows.append({f"col{i}": v for i, v in enumerate(record)})
        else:
            rows.append({"value": record})
    return rows


def _rows_from_table(table: list[list[str]]) -> list[dict]:
    if len(table) < 2:
        return []
    header = table[0]
    seen: dict[str, int] = {}
    names: list[str] = []
    for index, raw in enumerate(header):
        name = raw.strip() or f"col{index}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        names.append(name)

    rows = []
    for line in table[1:]:
        if len(line) == 1 and not line[0]:
            continue
        row = {names[i]: (line[i] if i < len(line) else "") for i in range(len(names))}
        for extra in range(len(names), len(line)):
            row[f"col{extra}"] = line[extra]
        rows.append(row)
    return rows


def _rows_from_csv(text: str) -> list[dict]:
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [{k or "": v for k, v in row.items()} for row in reader]


def parse_records(response: Response) -> list[dict]:
    """Turn any SISMET response into a list of flat records.

    Tries JSON, then the largest HTML table on the page, then raw CSV -- in
    that order -- so the caller never has to care what the endpoint served.
    """
    text = response.text()
    stripped = text.lstrip()

    if stripped[:1] in "[{":
        try:
            return _rows_from_json(json.loads(text))
        except json.JSONDecodeError:
            pass

    if "<" in stripped[:512].lower():
        parser = _TableParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:  # malformed markup should not abort the download
            pass
        tables = [t for t in parser.tables if len(t) >= 2]
        if tables:
            largest = max(tables, key=len)
            rows = _rows_from_table(largest)
            if rows:
                return rows
        return []

    if stripped:
        try:
            return _rows_from_csv(text)
        except csv.Error:
            return []
    return []


def parse_options(response: Response) -> list[tuple[str, str]]:
    """Extract (value, label) pairs from <select><option> station pickers."""
    parser = _OptionParser()
    try:
        parser.feed(response.text())
        parser.close()
    except Exception:
        return []
    return [(v, lbl) for v, lbl in parser.options if v.lower() not in ("", "0", "none", "null")]


# --------------------------------------------------------------------------
# Stations, windows, CSV output
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Station:
    code: str
    name: str

    def __str__(self) -> str:
        return f"{self.code:>6}  {self.name}" if self.name else str(self.code)


def iter_windows(
    start: dt.date, end: dt.date, days: int
) -> Iterator[tuple[dt.date, dt.date]]:
    """Split [start, end] into consecutive inclusive windows of ``days`` days.

    Long historic ranges (SISMET stations go back to 2010) are refused or
    truncated by most such endpoints, so requests are always chunked.
    """
    if end < start:
        raise ValueError("end date precedes start date")
    if days < 1:
        raise ValueError("window size must be at least one day")
    cursor = start
    step = dt.timedelta(days=days)
    one_day = dt.timedelta(days=1)
    while cursor <= end:
        window_end = min(cursor + step - one_day, end)
        yield cursor, window_end
        cursor = window_end + one_day


def _row_key(row: dict) -> str:
    """A stable identity for a row, comparable across a CSV round-trip.

    Values are stringified because a row read back from an existing CSV during
    a resume is all text, while a freshly parsed JSON row holds ints and
    floats -- ``1`` and ``"1"`` must hash alike or resumed runs would append
    every row a second time.  Empty values are dropped for the same reason: a
    missing key and a key the CSV writer padded with "" are the same row.
    """
    normalised = {
        str(key): str(value).strip()
        for key, value in row.items()
        if value is not None and str(value).strip() != ""
    }
    return json.dumps(normalised, sort_keys=True, ensure_ascii=False)


class CsvSink:
    """Streaming CSV writer that tolerates a changing set of fields.

    The header is fixed by the first batch of rows.  Fields that only turn up
    in a later window are written into a trailing ``extra_json`` column instead
    of being dropped, which keeps the write single-pass while staying lossless.
    """

    def __init__(self, path: str, append: bool = False, dedupe: bool = True):
        self.path = path
        self.dedupe = dedupe
        self.fieldnames: list[str] = []
        self.rows_written = 0
        self.duplicates_skipped = 0
        self._seen: set[str] = set()
        self._handle = None
        self._writer = None
        self._append = append and os.path.exists(path) and os.path.getsize(path) > 0

        if self._append:
            with open(path, "r", encoding="utf-8-sig", newline="") as existing:
                reader = csv.reader(existing)
                try:
                    self.fieldnames = next(reader)
                except StopIteration:
                    self.fieldnames = []
                if self.dedupe and self.fieldnames:
                    for line in reader:
                        record = dict(zip(self.fieldnames, line))
                        # Fold the overflow column back in so a row that
                        # carried extras still matches when it reappears.
                        extras = record.pop(EXTRA_COLUMN, "")
                        if extras:
                            try:
                                record.update(json.loads(extras))
                            except (json.JSONDecodeError, ValueError):
                                pass
                        self._seen.add(_row_key(record))
                        self.rows_written += 1

    def _open(self, first_row: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        if not self.fieldnames:
            self.fieldnames = list(first_row.keys()) + [EXTRA_COLUMN]
        mode = "a" if self._append else "w"
        self._handle = open(self.path, mode, encoding="utf-8-sig", newline="")
        self._writer = csv.DictWriter(
            self._handle, fieldnames=self.fieldnames, extrasaction="ignore"
        )
        if not self._append:
            self._writer.writeheader()

    def write(self, rows: Iterable[dict]) -> int:
        written = 0
        for row in rows:
            if self._writer is None:
                self._open(row)
            if self.dedupe:
                key = _row_key(row)
                if key in self._seen:
                    self.duplicates_skipped += 1
                    continue
                self._seen.add(key)
            known = set(self.fieldnames)
            extras = {k: v for k, v in row.items() if k not in known}
            payload = {k: v for k, v in row.items() if k in known}
            if extras:
                payload[EXTRA_COLUMN] = json.dumps(extras, ensure_ascii=False, default=str)
            self._writer.writerow(payload)
            written += 1
            self.rows_written += 1
        if self._handle:
            self._handle.flush()
        return written

    def close(self) -> None:
        if self._handle:
            self._handle.close()
            self._handle = None
            self._writer = None

    def __enter__(self) -> "CsvSink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@dataclass
class DateParams:
    """How this server wants a date range expressed."""

    start_name: str = DATE_PARAM_CANDIDATES[0][0]
    end_name: str = DATE_PARAM_CANDIDATES[0][1]
    date_format: str = DATE_FORMAT_CANDIDATES[0]

    def build(self, start: dt.date, end: dt.date) -> dict:
        return {
            self.start_name: start.strftime(self.date_format),
            self.end_name: end.strftime(self.date_format),
        }

    def describe(self) -> str:
        return f"{self.start_name}/{self.end_name} as {self.date_format}"


@dataclass
class SismetClient:
    http: HttpClient = field(default_factory=HttpClient)
    base_url: str = BASE_URL
    log: Optional[Callable[[str], None]] = None

    def _note(self, message: str) -> None:
        if self.log:
            self.log(message)

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def list_stations(self, dataset: Dataset) -> list[Station]:
        """Fetch the station/gauge list from the dataset's index page."""
        response = self.http.get(self._url(dataset.index_path))

        stations: list[Station] = []
        for value, label in parse_options(response):
            if value.isdigit() or re.fullmatch(r"[A-Za-z0-9_-]+", value):
                stations.append(Station(code=value, name=label))
        if stations:
            return _dedupe_stations(stations)

        # Fall back to a table listing: find the most code-like and name-like
        # columns rather than assuming a fixed layout.
        for row in parse_records(response):
            code = _first_matching(row, ("cdestacao", "cdpto", "codigo", "cod", "id", "code"))
            name = _first_matching(
                row, ("nome", "estacao", "descricao", "municipio", "local", "name")
            )
            if code:
                stations.append(Station(code=str(code).strip(), name=str(name or "").strip()))
        return _dedupe_stations(stations)

    def fetch_window(
        self,
        dataset: Dataset,
        code: str,
        start: dt.date,
        end: dt.date,
        date_params: Optional[DateParams],
        extra_params: Optional[dict] = None,
    ) -> list[dict]:
        """Fetch one date window of historic data for one station."""
        params = {dataset.id_param: code}
        if date_params is not None:
            params.update(date_params.build(start, end))
        if extra_params:
            params.update(extra_params)
        response = self.http.get(self._url(dataset.search_path), params=params)
        return parse_records(response)

    def probe_date_parameters(
        self,
        dataset: Dataset,
        code: str,
        start: dt.date,
        end: dt.date,
    ) -> Optional[DateParams]:
        """Discover which date-parameter spelling this server honours.

        Strategy: fetch the unfiltered endpoint as a baseline, then try each
        candidate over a narrow window.  A candidate is accepted when it
        returns rows and those rows differ from the baseline -- i.e. the
        filter demonstrably took effect.  A candidate that returns exactly the
        baseline is being ignored by the server and is rejected.
        """
        self._note(f"Probing date parameters against {dataset.search_path} ...")

        # Up to 33 combinations get tried; retrying each one three times with
        # exponential backoff would turn a dead endpoint into a ten-minute
        # wait, so probe requests get a single attempt each.
        original_retries = self.http.retries
        self.http.retries = 1
        try:
            try:
                baseline_rows = self.fetch_window(dataset, code, start, end, None)
            except (NetworkError, SismetError) as exc:
                # Some endpoints refuse an unfiltered query outright.  That is
                # not fatal: without a baseline we simply rely on the
                # in-range date check to accept a candidate.
                baseline_rows = []
                self._note(f"  baseline request failed ({exc}); continuing without it")
            baseline = {_row_key(r) for r in baseline_rows}
            self._note(f"  baseline (no date filter): {len(baseline_rows)} rows")
            return self._try_candidates(dataset, code, start, end, baseline)
        finally:
            self.http.retries = original_retries

    def _try_candidates(
        self,
        dataset: Dataset,
        code: str,
        start: dt.date,
        end: dt.date,
        baseline: set,
    ) -> Optional[DateParams]:
        for start_name, end_name in DATE_PARAM_CANDIDATES:
            for date_format in DATE_FORMAT_CANDIDATES:
                candidate = DateParams(start_name, end_name, date_format)
                try:
                    rows = self.fetch_window(dataset, code, start, end, candidate)
                except (NetworkError, SismetError) as exc:
                    self._note(f"  {candidate.describe()}: error ({exc})")
                    continue
                if not rows:
                    self._note(f"  {candidate.describe()}: no rows")
                    continue
                current = {_row_key(r) for r in rows}
                if baseline and current == baseline:
                    self._note(f"  {candidate.describe()}: ignored (identical to baseline)")
                    continue
                if _rows_within_range(rows, start, end):
                    self._note(f"  {candidate.describe()}: ACCEPTED ({len(rows)} rows in range)")
                    return candidate
                self._note(
                    f"  {candidate.describe()}: {len(rows)} rows, but dates outside the "
                    "requested range"
                )
        self._note("  no candidate accepted - see DATE_PARAM_CANDIDATES in sismet_client.py")
        return None


def _dedupe_stations(stations: list[Station]) -> list[Station]:
    seen: set[str] = set()
    unique: list[Station] = []
    for station in stations:
        if station.code in seen:
            continue
        seen.add(station.code)
        unique.append(station)
    return sorted(unique, key=lambda s: (len(s.code), s.code))


def _first_matching(row: dict, hints: Sequence[str]):
    normalised = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in row.items()}
    for hint in hints:
        if hint in normalised and str(normalised[hint]).strip():
            return normalised[hint]
    for key, value in normalised.items():
        if any(hint in key for hint in hints) and str(value).strip():
            return value
    return None


def find_timestamp_key(row: dict) -> Optional[str]:
    normalised = {k: re.sub(r"[^a-z0-9]", "", k.lower()) for k in row}
    for key, norm in normalised.items():
        if norm in TIMESTAMP_KEY_HINTS:
            return key
    for key, norm in normalised.items():
        if any(hint in norm for hint in TIMESTAMP_KEY_HINTS):
            return key
    return None


DATE_PATTERNS = (
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), (1, 2, 3)),
    (re.compile(r"(\d{2})/(\d{2})/(\d{4})"), (3, 2, 1)),
    (re.compile(r"(\d{2})-(\d{2})-(\d{4})"), (3, 2, 1)),
)


def extract_date(value) -> Optional[dt.date]:
    """Pull a date out of whatever the server put in the timestamp column."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "")
    for pattern, (y, m, d) in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return dt.date(int(match.group(y)), int(match.group(m)), int(match.group(d)))
            except ValueError:
                continue
    return None


def _rows_within_range(rows: list[dict], start: dt.date, end: dt.date) -> bool:
    """True if the rows carry dates and they all fall inside [start, end].

    When no date column can be identified the check is inconclusive, and we
    accept -- the caller has already established the response differs from the
    unfiltered baseline.
    """
    if not rows:
        return False
    key = find_timestamp_key(rows[0])
    if key is None:
        return True
    dates = [extract_date(row.get(key)) for row in rows]
    dates = [d for d in dates if d is not None]
    if not dates:
        return True
    slack = dt.timedelta(days=1)  # tolerate inclusive/exclusive edge handling
    return min(dates) >= start - slack and max(dates) <= end + slack
