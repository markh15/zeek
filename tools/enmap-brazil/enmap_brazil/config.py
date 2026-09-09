"""User-facing settings, with defaults tuned for a Brazil-wide decade sweep."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .regions import BRAZIL_BBOX, BBox
from .stac import DEFAULT_STAC_ROOT

DEFAULT_DB = Path(
    os.environ.get("ENMAP_BR_DB", Path.home() / ".local/share/enmap-brazil/enmap_brazil.sqlite")
)
DEFAULT_DOWNLOAD_DIR = Path(
    os.environ.get("ENMAP_BR_DOWNLOADS", Path.home() / ".local/share/enmap-brazil/downloads")
)
#: Requested history. EnMAP only launched in 2022, so the harvester clamps this
#: to each collection's advertised temporal extent -- ask for ten years, get
#: however much actually exists.
DEFAULT_YEARS = 10


def default_start() -> datetime:
    return utcnow() - timedelta(days=365 * DEFAULT_YEARS)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass
class Settings:
    stac_root: str = DEFAULT_STAC_ROOT
    db_path: Path = field(default_factory=lambda: DEFAULT_DB)
    download_dir: Path = field(default_factory=lambda: DEFAULT_DOWNLOAD_DIR)
    collections: list[str] = field(default_factory=list)
    bbox: BBox = BRAZIL_BBOX
    start: datetime = field(default_factory=default_start)
    end: datetime = field(default_factory=utcnow)
    window_months: int = 3
    grid_deg: float | None = None
    page_limit: int = 100
    rate_limit: float = 0.2
    timeout: float = 60.0
    max_retries: int = 5
    resume: bool = True
    queue_assets: bool = False
    asset_roles: list[str] = field(default_factory=lambda: ["data"])

    def with_(self, **kwargs: Any) -> "Settings":
        return replace(self, **kwargs)

    def to_json(self) -> str:
        data = asdict(self)
        data["db_path"] = str(self.db_path)
        data["download_dir"] = str(self.download_dir)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        data["bbox"] = list(self.bbox)
        return json.dumps(data, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key in ("db_path", "download_dir"):
                kwargs[key] = Path(value)
            elif key in ("start", "end"):
                kwargs[key] = datetime.fromisoformat(value)
            elif key == "bbox":
                kwargs[key] = tuple(float(v) for v in value)
            elif key in {f.name for f in cls.__dataclass_fields__.values()}:
                kwargs[key] = value
        return cls(**kwargs)

    def auth(self) -> tuple[str, str] | None:
        """Credentials for product downloads (an EOWEB/DLR account)."""
        user = os.environ.get("ENMAP_BR_USER") or os.environ.get("EOWEB_USER")
        password = os.environ.get("ENMAP_BR_PASSWORD") or os.environ.get("EOWEB_PASSWORD")
        if user and password:
            return (user, password)
        return None
