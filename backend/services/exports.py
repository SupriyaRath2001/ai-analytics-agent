"""Files the chat links to: CSV downloads and chart PNGs.

Names are random (uuid4 hex), so links can't be guessed, and files are deleted once they are
older than ``ttl_hours``. The API serves them at ``/downloads/<name>``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Only names this store creates are accepted, which also blocks paths like "../config.yaml".
_FILE_NAME = re.compile(r"[0-9a-f]{32}\.(csv|png)")


class ExportStore:
    def __init__(self, export_dir: str | Path, public_base_url: str = "", ttl_hours: int = 24):
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self.ttl_hours = ttl_hours

    def save_csv(self, df: pd.DataFrame) -> str:
        """Save the rows and return the file name. The BOM makes Excel read the file as UTF-8."""
        file_name = f"{uuid.uuid4().hex}.csv"
        df.to_csv(self.export_dir / file_name, index=False, encoding="utf-8-sig")
        return file_name

    def save_png(self, data: bytes) -> str:
        file_name = f"{uuid.uuid4().hex}.png"
        (self.export_dir / file_name).write_bytes(data)
        return file_name

    def url_for(self, file_name: str) -> str:
        return f"{self.public_base_url}/downloads/{file_name}"

    def path_for(self, file_name: str) -> Path | None:
        """The file's path, or None if the name is malformed or the file is missing or expired."""
        # fullmatch, not "^...$": "$" would also accept a name followed by a newline.
        if not _FILE_NAME.fullmatch(file_name):
            return None
        path = self.export_dir / file_name
        if self._is_expired(path):
            return None
        return path

    def cleanup(self) -> int:
        """Delete expired files and return how many were deleted. Other files are left alone."""
        removed = 0
        for path in self.export_dir.iterdir():
            if not _FILE_NAME.fullmatch(path.name) or not self._is_expired(path):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue  # already deleted, e.g. by a cleanup running at the same time
            except OSError as exc:
                log.warning("could not delete expired export %s: %s", path, exc)
                continue
            removed += 1
        if removed:
            log.info("deleted %d expired export file(s) from %s", removed, self.export_dir)
        return removed

    def _is_expired(self, path: Path) -> bool:
        """True when the file is older than the TTL. A missing file counts as expired."""
        try:
            modified = path.stat().st_mtime
        except FileNotFoundError:
            return True
        return time.time() - modified > self.ttl_hours * 3600
