import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from enmap_brazil.db import Database
from enmap_brazil.download import Downloader, asset_rows, safe_name

PAYLOAD = b"hyperspectral-bytes" * 500


class _FileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    supports_range = True

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range")
        if rng and self.supports_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(PAYLOAD):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD)-1}/{len(PAYLOAD)}")
        else:
            self.send_response(200)
        body = PAYLOAD[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def file_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_asset_rows_filters_by_role():
    assets = {
        "data": {"href": "http://x/1.zip", "roles": ["data"], "type": "application/zip"},
        "thumbnail": {"href": "http://x/1.png", "roles": ["thumbnail"]},
        "broken": {"roles": ["data"]},
    }
    rows = asset_rows("C", "ITEM", assets, {"data"})
    assert [r["key"] for r in rows] == ["data"]
    assert rows[0]["media_type"] == "application/zip"
    assert len(asset_rows("C", "ITEM", assets, set())) == 2, "no filter keeps everything with an href"


def test_safe_name_strips_path_separators_and_keeps_the_suffix():
    name = safe_name("ENMAP01/../etc", "data", "http://h/a/b/product.tar.gz")
    assert "/" not in name and ".." not in name
    assert name.endswith(".tar.gz")


def test_download_writes_the_file_and_records_it(file_server, db_path, tmp_path):
    with Database(db_path) as db:
        db.queue_assets([{"collection": "C", "item_id": "ITEM", "key": "data", "href": f"{file_server}/p.zip"}])
        downloader = Downloader(db, tmp_path / "dl")
        try:
            tally = downloader.run()
        finally:
            downloader.close()
        row = db.conn.execute("SELECT * FROM assets").fetchone()
    assert tally == {"done": 1, "error": 0, "skipped": 0}
    assert row["status"] == "done"
    assert row["file_size"] == len(PAYLOAD)
    assert len(row["sha256"]) == 64
    from pathlib import Path

    assert Path(row["local_path"]).read_bytes() == PAYLOAD


def test_download_resumes_a_partial_file(file_server, db_path, tmp_path):
    target_dir = tmp_path / "dl" / "C"
    target_dir.mkdir(parents=True)
    with Database(db_path) as db:
        downloader = Downloader(db, tmp_path / "dl")
        name = safe_name("ITEM", "data", f"{file_server}/p.zip")
        part = target_dir / (name + ".part")
        part.write_bytes(PAYLOAD[:100])
        try:
            path, size, _ = downloader.fetch(f"{file_server}/p.zip", "ITEM", "data", "C")
        finally:
            downloader.close()
    assert size == len(PAYLOAD)
    assert path.read_bytes() == PAYLOAD, "resumed bytes must not be duplicated or lost"
    assert not part.exists()


def test_download_restarts_when_the_server_ignores_range(file_server, db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(_FileHandler, "supports_range", False)
    target_dir = tmp_path / "dl" / "C"
    target_dir.mkdir(parents=True)
    with Database(db_path) as db:
        downloader = Downloader(db, tmp_path / "dl")
        name = safe_name("ITEM", "data", f"{file_server}/p.zip")
        (target_dir / (name + ".part")).write_bytes(b"stale-garbage")
        try:
            path, size, _ = downloader.fetch(f"{file_server}/p.zip", "ITEM", "data", "C")
        finally:
            downloader.close()
    assert path.read_bytes() == PAYLOAD
    assert size == len(PAYLOAD)


def test_existing_file_is_not_downloaded_again(file_server, db_path, tmp_path):
    with Database(db_path) as db:
        downloader = Downloader(db, tmp_path / "dl")
        try:
            first, _, _ = downloader.fetch(f"{file_server}/p.zip", "ITEM", "data", "C")
            first.write_bytes(b"already here")
            second, size, _ = downloader.fetch(f"{file_server}/p.zip", "ITEM", "data", "C")
        finally:
            downloader.close()
    assert second == first
    assert second.read_bytes() == b"already here", "an existing file is left alone"
    assert size == len(b"already here")


def test_failed_download_is_recorded_not_raised(file_server, db_path, tmp_path):
    with Database(db_path) as db:
        db.queue_assets([{"collection": "C", "item_id": "I", "key": "data", "href": f"{file_server}/missing"}])
        downloader = Downloader(db, tmp_path / "dl")
        try:
            tally = downloader.run()
        finally:
            downloader.close()
        row = db.conn.execute("SELECT status, error FROM assets").fetchone()
    assert tally["error"] == 1
    assert row["status"] == "error" and "404" in row["error"]
    # An errored asset stays in the queue for the next attempt.
    with Database(db_path) as db:
        assert len(db.pending_assets()) == 1


def test_stop_event_aborts_the_download(file_server, db_path, tmp_path):
    stop = threading.Event()
    stop.set()
    with Database(db_path) as db:
        downloader = Downloader(db, tmp_path / "dl", stop_event=stop)
        try:
            with pytest.raises(RuntimeError, match="stopped"):
                downloader.fetch(f"{file_server}/p.zip", "ITEM", "data", "C")
        finally:
            downloader.close()
    written = sorted(p.name for p in (tmp_path / "dl" / "C").iterdir())
    # Only a .part file may exist: an interrupted download is never promoted
    # to the final name, so a later run cannot mistake it for a complete file.
    assert all(name.endswith(".part") for name in written), written
