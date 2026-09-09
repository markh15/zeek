"""An in-process STAC API good enough to exercise the client end to end."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

COLLECTION_ID = "ENMAP_HSI_L2A"
COLLECTION_START = datetime(2022, 11, 2, tzinfo=timezone.utc)
COLLECTION_END = datetime(2025, 6, 1, tzinfo=timezone.utc)


def make_item(index: int, when: datetime, lon: float = -60.0, lat: float = -10.0) -> dict:
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": f"ENMAP01-____L2A-DT{index:010d}",
        "collection": COLLECTION_ID,
        "bbox": [lon, lat, lon + 0.3, lat + 0.3],
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [lon, lat],
                    [lon + 0.3, lat],
                    [lon + 0.3, lat + 0.3],
                    [lon, lat + 0.3],
                    [lon, lat],
                ]
            ],
        },
        "properties": {
            "datetime": when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "eo:cloud_cover": (index * 7) % 100,
            "view:sun_elevation": 45.5,
            "platform": "enmap",
            "instruments": ["hsi"],
            "processing:level": "L2A",
            "gsd": 30,
        },
        "assets": {
            "data": {
                "href": f"http://example.invalid/products/{index}.zip",
                "type": "application/zip",
                "roles": ["data"],
                "file:size": 1024,
            },
            "thumbnail": {
                "href": f"http://example.invalid/thumbs/{index}.png",
                "type": "image/png",
                "roles": ["thumbnail"],
            },
        },
        "links": [{"rel": "self", "href": f"http://example.invalid/items/{index}"}],
    }


class FakeStacServer:
    """Serves a deterministic catalogue; records every request for assertions."""

    def __init__(
        self,
        item_count: int = 25,
        page_size: int = 10,
        allow_post: bool = True,
        fail_times: int = 0,
    ) -> None:
        self.item_count = item_count
        self.page_size = page_size
        self.allow_post = allow_post
        self.fail_times = fail_times
        self.requests: list[tuple[str, str]] = []
        self._failures = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- data --------------------------------------------------------------
    def items(self) -> list[dict]:
        base = COLLECTION_START + timedelta(days=1)
        return [
            make_item(i, base + timedelta(days=i * 11), lon=-70.0 + i * 1.5)
            for i in range(self.item_count)
        ]

    def _matching(self, dt_range: str | None) -> list[dict]:
        items = self.items()
        if not dt_range or "/" not in dt_range:
            return items
        lo_text, hi_text = dt_range.split("/", 1)
        lo = _parse(lo_text)
        hi = _parse(hi_text)
        out = []
        for item in items:
            when = _parse(item["properties"]["datetime"])
            if when is None:
                continue
            if lo and when < lo:
                continue
            if hi and when > hi:
                continue
            out.append(item)
        return out

    def _page(self, items: list[dict], offset: int, base_url: str, method: str) -> dict:
        window = items[offset : offset + self.page_size]
        next_offset = offset + self.page_size
        links = []
        if next_offset < len(items):
            if method == "POST":
                links.append(
                    {
                        "rel": "next",
                        "href": f"{base_url}/search",
                        "method": "POST",
                        "body": {"offset": next_offset},
                    }
                )
            else:
                links.append(
                    {"rel": "next", "href": f"{base_url}/search?offset={next_offset}"}
                )
        return {
            "type": "FeatureCollection",
            "features": window,
            "numberMatched": len(items),
            "numberReturned": len(window),
            "links": links,
        }

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "FakeStacServer":
        self.url = self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _parse(text: str) -> datetime | None:
    text = text.strip()
    if not text or text == "..":
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        rest = tail[len(digits) :] if not tail[len(digits) :].isdigit() else ""
        text = f"{head}.{digits or '0'}{rest}"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _make_handler(state: FakeStacServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # silence stderr spam
            pass

        @property
        def base_url(self) -> str:
            return f"http://{self.headers.get('Host')}"

        def _send(self, status: int, payload: dict | str) -> None:
            body = (
                json.dumps(payload).encode() if isinstance(payload, dict) else payload.encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _maybe_fail(self) -> bool:
            if state._failures < state.fail_times:
                state._failures += 1
                self._send(503, {"error": "try again"})
                return True
            return False

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            state.requests.append(("GET", self.path))
            if self._maybe_fail():
                return
            query = parse_qs(parsed.query)
            offset = int(query.get("offset", ["0"])[0])
            dt = query.get("datetime", [None])[0]

            if parsed.path == "/collections":
                self._send(
                    200,
                    {
                        "collections": [
                            {
                                "id": COLLECTION_ID,
                                "title": "EnMAP HSI Level 2A",
                                "description": "Atmospherically corrected hyperspectral imagery",
                                "license": "proprietary",
                                "extent": {
                                    "spatial": {"bbox": [[-180, -90, 180, 90]]},
                                    "temporal": {
                                        "interval": [
                                            [
                                                COLLECTION_START.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                COLLECTION_END.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                            ]
                                        ]
                                    },
                                },
                            },
                            {
                                "id": "TDM90_DEM",
                                "title": "TanDEM-X 90m DEM",
                                "description": "Global elevation model",
                                "extent": {},
                            },
                        ],
                        "links": [],
                    },
                )
                return

            if parsed.path == "/search":
                self._send(200, state._page(state._matching(dt), offset, self.base_url, "GET"))
                return

            if parsed.path.startswith("/collections/") and parsed.path.endswith("/items"):
                self._send(200, state._page(state._matching(dt), offset, self.base_url, "GET"))
                return

            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            state.requests.append(("POST", self.path))
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if not state.allow_post:
                self._send(405, {"error": "POST search not supported"})
                return
            if self._maybe_fail():
                return
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            offset = int(body.get("offset", 0))
            self._send(
                200,
                state._page(
                    state._matching(body.get("datetime")), offset, self.base_url, "POST"
                ),
            )

    return Handler
