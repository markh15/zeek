"""Optional product downloader for queued STAC assets.

Metadata harvesting is open; fetching EnMAP L1B/L1C/L2A products from DLR needs
an EOWEB account, supplied through ``ENMAP_BR_USER`` / ``ENMAP_BR_PASSWORD``.
Downloads stream to a ``.part`` file, resume with HTTP Range on restart, and are
renamed into place only once the bytes are on disk.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import httpx

from .db import Database

log = logging.getLogger(__name__)

CHUNK_BYTES = 1 << 20
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DOTS = re.compile(r"\.{2,}")


@dataclass
class DownloadProgress:
    item_id: str
    key: str
    downloaded: int
    total: int | None
    path: Path

    @property
    def fraction(self) -> float:
        if not self.total:
            return 0.0
        return min(1.0, self.downloaded / self.total)


def asset_rows(
    collection: str,
    item_id: str,
    assets: dict[str, Any] | None,
    roles: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten a STAC item's ``assets`` map into rows for the assets table.

    ``roles`` filters by STAC asset role (``{"data"}`` keeps products and drops
    thumbnails and metadata sidecars); an empty set keeps everything.
    """
    rows: list[dict[str, Any]] = []
    for key, asset in (assets or {}).items():
        if not isinstance(asset, dict) or not asset.get("href"):
            continue
        asset_roles = {str(r).lower() for r in (asset.get("roles") or [])}
        if roles and not (asset_roles & roles):
            continue
        rows.append(
            {
                "collection": collection,
                "item_id": item_id,
                "key": key,
                "href": asset["href"],
                "media_type": asset.get("type"),
                "title": asset.get("title"),
                "roles": sorted(asset_roles) or None,
                "file_size": asset.get("file:size"),
            }
        )
    return rows


def safe_name(item_id: str, key: str, href: str) -> str:
    """Build a filesystem-safe name, preferring the server's own filename."""
    tail = Path(unquote(urlparse(href).path)).name
    suffix = "".join(Path(tail).suffixes[-2:]) if tail else ""
    base = _UNSAFE.sub("_", f"{item_id}_{key}")
    # No separators survive the substitution, but collapse dot runs anyway so
    # the result can never look like a relative path component.
    base = _DOTS.sub("_", base).strip("_.") or "asset"
    if tail and _UNSAFE.sub("_", tail) not in base:
        return f"{base}{suffix}"
    return base


class Downloader:
    """Sequential, resumable asset downloader driven by the ``assets`` table."""

    def __init__(
        self,
        db: Database,
        download_dir: Path,
        *,
        auth: tuple[str, str] | None = None,
        timeout: float = 300.0,
        verify_hash: bool = True,
        on_progress: Callable[[DownloadProgress], None] | None = None,
        stop_event: threading.Event | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.db = db
        self.download_dir = Path(download_dir).expanduser()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.verify_hash = verify_hash
        self.on_progress = on_progress or (lambda p: None)
        self.stop_event = stop_event or threading.Event()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True, auth=auth
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def run(self, limit: int = 100) -> dict[str, int]:
        """Download up to ``limit`` pending assets. Returns a status tally."""
        tally = {"done": 0, "error": 0, "skipped": 0}
        for asset in self.db.pending_assets(limit=limit):
            if self.stop_event.is_set():
                break
            try:
                path, size, digest = self.fetch(
                    asset["href"], asset["item_id"], asset["key"], asset["collection"]
                )
            except Exception as exc:  # noqa: BLE001 - recorded per asset
                log.warning("download failed for %s/%s: %s", asset["item_id"], asset["key"], exc)
                self.db.update_asset(asset["asset_key"], status="error", error=str(exc))
                tally["error"] += 1
                continue
            self.db.update_asset(
                asset["asset_key"],
                status="done",
                local_path=str(path),
                file_size=size,
                sha256=digest,
                error=None,
            )
            tally["done"] += 1
        return tally

    def fetch(
        self, href: str, item_id: str, key: str, collection: str = ""
    ) -> tuple[Path, int, str | None]:
        """Fetch one asset, resuming a partial file when the server allows it."""
        target_dir = self.download_dir / _UNSAFE.sub("_", collection or "assets")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name(item_id, key, href)
        part = target.with_suffix(target.suffix + ".part")

        if target.exists():
            size = target.stat().st_size
            return (target, size, self._digest(target) if self.verify_hash else None)

        offset = part.stat().st_size if part.exists() else 0
        headers: dict[str, Any] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"

        with self._client.stream("GET", href, headers=headers) as response:
            if offset and response.status_code == 200:
                # Server ignored the Range header, so start over.
                offset = 0
                part.unlink(missing_ok=True)
            elif offset and response.status_code == 416:
                part.rename(target)
                size = target.stat().st_size
                return (target, size, self._digest(target) if self.verify_hash else None)
            response.raise_for_status()

            total = _content_total(response, offset)
            downloaded = offset
            mode = "ab" if offset else "wb"
            with part.open(mode) as handle:
                for block in response.iter_bytes(CHUNK_BYTES):
                    if self.stop_event.is_set():
                        raise RuntimeError("stopped before the download finished")
                    handle.write(block)
                    downloaded += len(block)
                    self.on_progress(
                        DownloadProgress(item_id, key, downloaded, total, target)
                    )

        part.replace(target)
        size = target.stat().st_size
        return (target, size, self._digest(target) if self.verify_hash else None)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
                digest.update(block)
        return digest.hexdigest()


def _content_total(response: httpx.Response, offset: int) -> int | None:
    length = response.headers.get("Content-Length")
    if length is None:
        return None
    try:
        return int(length) + offset
    except ValueError:
        return None
