"""A small, defensive STAC client aimed at the DLR EOC Geoservice catalogue.

The EnMAP catalogue is a standard STAC API, but different deployments enable
different pieces of it: some support ``POST /search``, some only ``GET /search``,
and some only the per-collection ``/items`` endpoint. Rather than guessing, the
client probes in that order and remembers what worked.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

#: DLR Earth Observation Center Geoservice, which publishes the EnMAP archive.
DEFAULT_STAC_ROOT = "https://geoservice.dlr.de/eoc/ogc/stac/v1"

#: Substrings used to spot EnMAP collections in a catalogue that also carries
#: TanDEM-X, Sentinel and friends.
ENMAP_HINTS = ("enmap", "en_map", "hsi")

USER_AGENT = "enmap-brazil/1.0 (+https://github.com/markh15/zeek)"


class StacError(RuntimeError):
    """Raised for non-retryable protocol or transport failures."""


@dataclass
class Collection:
    id: str
    title: str = ""
    description: str = ""
    license: str = ""
    temporal_start: datetime | None = None
    temporal_end: datetime | None = None
    bbox: list[float] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def temporal_extent(self) -> tuple[datetime | None, datetime | None]:
        return (self.temporal_start, self.temporal_end)

    @classmethod
    def from_json(cls, doc: dict[str, Any]) -> "Collection":
        extent = doc.get("extent") or {}
        temporal = (extent.get("temporal") or {}).get("interval") or []
        spatial = (extent.get("spatial") or {}).get("bbox") or []
        lo = hi = None
        if temporal and isinstance(temporal[0], (list, tuple)) and len(temporal[0]) == 2:
            lo = parse_datetime(temporal[0][0])
            hi = parse_datetime(temporal[0][1])
        bbox = None
        if spatial and isinstance(spatial[0], (list, tuple)):
            bbox = [float(v) for v in spatial[0]]
        return cls(
            id=str(doc.get("id", "")),
            title=str(doc.get("title") or ""),
            description=str(doc.get("description") or ""),
            license=str(doc.get("license") or ""),
            temporal_start=lo,
            temporal_end=hi,
            bbox=bbox,
            raw=doc,
        )


@dataclass
class Page:
    """One page of search results plus how to ask for the next one."""

    features: list[dict[str, Any]]
    next_request: "Request | None"
    matched: int | None = None


@dataclass
class Request:
    method: str
    url: str
    params: dict[str, Any] | None = None
    json_body: dict[str, Any] | None = None


def parse_datetime(value: Any) -> datetime | None:
    """Parse the RFC 3339 flavours STAC servers actually emit."""
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Trim over-long fractional seconds, which fromisoformat rejects.
    if "." in text:
        head, _, tail = text.partition(".")
        split = len(tail)
        for index, ch in enumerate(tail):
            if not ch.isdigit():
                split = index
                break
        digits, tail_rest = tail[:split][:6], tail[split:]
        text = f"{head}.{digits or '0'}{tail_rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            log.debug("unparseable datetime %r", value)
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class StacClient:
    """Paginating STAC reader with retries, backoff and polite rate limiting."""

    def __init__(
        self,
        root: str = DEFAULT_STAC_ROOT,
        *,
        timeout: float = 60.0,
        max_retries: int = 5,
        rate_limit: float = 0.2,
        auth: tuple[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.root = root.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate_limit = max(0.0, rate_limit)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            auth=auth,
        )
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._search_mode: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "StacClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------
    def _throttle(self) -> None:
        if not self.rate_limit:
            return
        with self._lock:
            wait = self.rate_limit - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def request(self, req: Request) -> dict[str, Any]:
        """Perform one request, retrying 429/5xx and transport errors."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self._client.request(
                    req.method,
                    req.url,
                    params=req.params,
                    json=req.json_body,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                self._sleep_backoff(attempt, None)
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                last_error = StacError(
                    f"{response.status_code} from {req.url}: {response.text[:200]}"
                )
                if attempt >= self.max_retries:
                    break
                self._sleep_backoff(attempt, response.headers.get("Retry-After"))
                continue

            if response.status_code >= 400:
                raise StacError(
                    f"{req.method} {req.url} failed with HTTP "
                    f"{response.status_code}: {response.text[:300]}"
                )
            try:
                return response.json()
            except ValueError as exc:
                raise StacError(f"{req.url} returned non-JSON content") from exc

        raise StacError(f"{req.method} {req.url} failed after retries: {last_error}")

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = min(60.0, (2.0**attempt)) + random.uniform(0, 0.5)
        log.debug("backing off %.1fs (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    # -- catalogue ---------------------------------------------------------
    def collections(self) -> list[Collection]:
        """Fetch every collection, following ``rel=next`` if the list pages."""
        url = f"{self.root}/collections"
        out: list[Collection] = []
        seen: set[str] = set()
        while url and url not in seen:
            seen.add(url)
            doc = self.request(Request("GET", url))
            for raw in doc.get("collections") or []:
                out.append(Collection.from_json(raw))
            url = _find_link(doc.get("links"), "next") or ""
        return out

    def enmap_collections(
        self,
        hints: Sequence[str] = ENMAP_HINTS,
        collections: Sequence[Collection] | None = None,
    ) -> list[Collection]:
        """Collections that look like EnMAP products.

        Matching on substrings rather than a hard-coded id keeps the tool
        working when DLR adds, renames or versions a product level. Pass
        ``collections`` to filter a list you already fetched.
        """
        lowered = tuple(h.lower() for h in hints)
        matched = []
        for coll in self.collections() if collections is None else collections:
            haystack = f"{coll.id} {coll.title} {coll.description}".lower()
            if any(h in haystack for h in lowered):
                matched.append(coll)
        # An id match is a much stronger signal than a description match.
        matched.sort(key=lambda c: (not any(h in c.id.lower() for h in lowered), c.id))
        return matched

    # -- search ------------------------------------------------------------
    def search_page(
        self,
        *,
        collections: Sequence[str],
        bbox: Sequence[float] | None = None,
        datetime_range: str | None = None,
        limit: int = 100,
    ) -> Page:
        """Fetch the first page of a search, probing the supported endpoints."""
        modes = [self._search_mode] if self._search_mode else ["post", "get", "items"]
        errors: list[str] = []
        for mode in modes:
            try:
                req = self._build_search_request(
                    mode, collections, bbox, datetime_range, limit
                )
                doc = self.request(req)
            except StacError as exc:
                errors.append(f"{mode}: {exc}")
                continue
            if "features" not in doc:
                errors.append(f"{mode}: response had no 'features' key")
                continue
            self._search_mode = mode
            return self._page_from(doc, mode)
        raise StacError("no usable search endpoint; tried " + " | ".join(errors))

    def _build_search_request(
        self,
        mode: str,
        collections: Sequence[str],
        bbox: Sequence[float] | None,
        datetime_range: str | None,
        limit: int,
    ) -> Request:
        if mode == "post":
            body: dict[str, Any] = {"limit": limit}
            if collections:
                body["collections"] = list(collections)
            if bbox:
                body["bbox"] = [float(v) for v in bbox]
            if datetime_range:
                body["datetime"] = datetime_range
            return Request("POST", f"{self.root}/search", json_body=body)

        params: dict[str, Any] = {"limit": limit}
        if bbox:
            params["bbox"] = ",".join(f"{float(v):.6f}" for v in bbox)
        if datetime_range:
            params["datetime"] = datetime_range

        if mode == "get":
            if collections:
                params["collections"] = ",".join(collections)
            return Request("GET", f"{self.root}/search", params=params)

        if mode == "items":
            if len(collections) != 1:
                raise StacError(
                    "the /items fallback handles one collection at a time "
                    f"(got {len(collections)})"
                )
            return Request(
                "GET", f"{self.root}/collections/{collections[0]}/items", params=params
            )

        raise StacError(f"unknown search mode {mode!r}")

    def _page_from(self, doc: dict[str, Any], mode: str) -> Page:
        features = [f for f in (doc.get("features") or []) if isinstance(f, dict)]
        matched = doc.get("numberMatched")
        if matched is None:
            matched = (doc.get("context") or {}).get("matched")
        return Page(
            features=features,
            next_request=self._next_request(doc, mode),
            matched=int(matched) if isinstance(matched, (int, float)) else None,
        )

    def _next_request(self, doc: dict[str, Any], mode: str) -> Request | None:
        for link in doc.get("links") or []:
            if not isinstance(link, dict) or link.get("rel") != "next":
                continue
            href = link.get("href")
            if not href:
                continue
            href = urljoin(self.root + "/", href)
            method = str(link.get("method") or ("POST" if mode == "post" else "GET")).upper()
            if method == "POST":
                body = link.get("body")
                if not isinstance(body, dict):
                    body = {}
                return Request("POST", href, json_body=body)
            return Request("GET", href)
        return None

    def iter_search(
        self,
        *,
        collections: Sequence[str],
        bbox: Sequence[float] | None = None,
        datetime_range: str | None = None,
        limit: int = 100,
        max_pages: int = 10_000,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[Page]:
        """Yield every page of a search, guarding against pagination loops."""
        page = self.search_page(
            collections=collections,
            bbox=bbox,
            datetime_range=datetime_range,
            limit=limit,
        )
        yield page
        seen: set[str] = set()
        pages = 1
        while page.next_request and pages < max_pages:
            if should_stop and should_stop():
                return
            req = page.next_request
            fingerprint = f"{req.method} {req.url} {sorted((req.json_body or {}).items())!r}"
            if fingerprint in seen:
                log.warning("stopping: server repeated the same 'next' link")
                return
            seen.add(fingerprint)
            doc = self.request(req)
            page = self._page_from(doc, self._search_mode or "get")
            pages += 1
            if not page.features and not page.next_request:
                return
            yield page


def _find_link(links: Any, rel: str) -> str | None:
    for link in links or []:
        if isinstance(link, dict) and link.get("rel") == rel and link.get("href"):
            return str(link["href"])
    return None
