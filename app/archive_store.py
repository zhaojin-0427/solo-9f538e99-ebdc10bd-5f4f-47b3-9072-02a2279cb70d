"""Durable, atomic archive storage for reviewed almanac tables.

Each archive is one JSON document named after its archive number.  Writes go
to a temporary file in the same directory, are flushed and fsynced, and only
then atomically renamed over the previous document.  If anything fails the
previous archive document remains untouched.
"""
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote


class ArchiveWriteError(RuntimeError):
    """Persisting an archive document failed; the old state is intact."""


_ARCHIVE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def validate_archive_id(archive_id: str) -> str:
    """Archive numbers are caller-chosen; keep them filesystem-safe."""
    if not isinstance(archive_id, str) or not _ARCHIVE_ID.match(archive_id):
        raise ValueError(
            "archive id must be 1-200 characters of letters, digits, "
            "underscore, dot, colon or hyphen"
        )
    if archive_id in (".", ".."):
        raise ValueError("invalid archive id")
    return archive_id


class ArchiveStore:
    """One JSON document per archive, addressed by archive number.

    The directory is resolved on every access so that ``ARCHIVE_DIR`` can be
    changed (or monkeypatched in tests) after import.
    """

    def __init__(self, directory: Optional[str] = None):
        self._directory = directory
        self.lock = threading.RLock()

    @property
    def directory(self) -> Path:
        if self._directory is not None:
            return Path(self._directory)
        return Path(os.environ.get("ARCHIVE_DIR", "data/archives"))

    def _path(self, archive_id: str) -> Path:
        return self.directory / f"{quote(archive_id, safe='')}.json"

    def load(self, archive_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(archive_id)
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ArchiveWriteError(f"cannot read archive: {exc}") from exc

    def list_ids(self) -> list:
        try:
            entries = sorted(self.directory.glob("*.json"))
        except OSError:
            return []
        return [unquote(entry.stem) for entry in entries]

    def save(self, archive_id: str, record: Dict[str, Any]) -> None:
        with self.lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ArchiveWriteError(
                    f"cannot create archive directory: {exc}"
                ) from exc
            target = self._path(archive_id)
            tmp_name = None
            try:
                safe_name = quote(archive_id, safe="")
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{safe_name}.", suffix=".tmp", dir=self.directory
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_name, 0o644)
                os.replace(tmp_name, target)
                tmp_name = None
                self._fsync_directory()
            except (OSError, TypeError, ValueError) as exc:
                raise ArchiveWriteError(f"cannot write archive: {exc}") from exc
            finally:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass

    def _fsync_directory(self) -> None:
        try:
            fd = os.open(self.directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
