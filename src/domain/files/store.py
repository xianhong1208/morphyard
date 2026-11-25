"""Result store: files kept on the server for a short time (issue #33).

A small `ResultStore` interface with one implementation, `LocalStore`, a directory on disk.
Tools put results here (PR 3) and hand out signed download links (`links.py`); later PRs add
uploads and job results. S3 or similar can implement the same interface without touching tools.

Layout under the store directory::

    <owner_key>/<file_id>/blob        the bytes
    <owner_key>/<file_id>/meta.json   StoredFile as JSON
    .key_id                           fingerprint of the signing key the links were made with

* `owner_key` = sha256 of the owner's token `sub` (first 32 hex digits): no raw identity in paths.
* `file_id` = `f_` + 128 random bits, URL-safe. Ids from outside are checked against that exact
  shape before they touch the file system, so `../` or an absolute path can never be one.
* An entry is written as `<file_id>.tmp/` and renamed into place, so a crash never leaves a half
  file under a valid id; leftovers are removed by the startup scan.
* Every file has an expiry: the default TTL (1 h), a caller may ask for less, never more, and never
  more than 24 h.
* Caps: total and per owner. A write that would go over sweeps expired files first and, if still
  over, is refused with `StoreFullError`. An unexpired file is never evicted: a link that was handed
  out keeps working until the time it says it expires.

The index of live files is kept in memory (rebuilt from disk at startup), so the store belongs to
one server process. Methods are synchronous file I/O; async callers use `asyncio.to_thread`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from src.domain.exceptions import DomainException
from src.log import get_server_logger

logger = get_server_logger()

MAX_TTL_SEC = 24 * 3600
DEFAULT_TTL_SEC = 3600
FileKind = Literal["result", "upload", "job"]

_FILE_ID_RE = re.compile(r"^f_[A-Za-z0-9_-]{22}$")
_OWNER_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_MIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$")
_MAX_NAME_BYTES = 200
_COPY_CHUNK = 1024 * 1024


class StoreFullError(DomainException):
    """The store (or this owner's share of it) has no room for the file."""

    def __init__(self, message: str, *, scope: Literal["total", "owner"], retry_after_sec: int,
                 never_fits: bool = False):
        super().__init__(message, "storage_full", {"scope": scope, "retry_after_sec": retry_after_sec,
                                                   "never_fits": never_fits})


@dataclass(frozen=True)
class StoredFile:
    """What the store knows about one file (also its meta.json)."""
    file_id: str
    owner_key: str
    name: str
    mime: str
    size: int
    sha256: str
    created_at: float
    expires_at: float
    kind: str = "result"

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


@dataclass(frozen=True)
class SweepResult:
    removed: int
    freed_bytes: int


class ResultStore(Protocol):
    """What tools and the download endpoint need from a store."""

    def put_bytes(self, owner: str, data: bytes, *, name: str, mime: str,
                  ttl_sec: int | None = None, kind: FileKind = "result") -> StoredFile: ...

    def put_file(self, owner: str, path: str | os.PathLike, *, name: str, mime: str,
                 ttl_sec: int | None = None, kind: FileKind = "result") -> StoredFile: ...

    def get(self, file_id: str, owner: str | None = None) -> StoredFile | None: ...

    def open(self, file_id: str) -> BinaryIO: ...

    def blob_path(self, file_id: str) -> Path | None: ...

    def delete(self, file_id: str, owner: str | None = None) -> bool: ...

    def sweep(self, now: float | None = None) -> SweepResult: ...

    def purge_all(self) -> int: ...

    def stats(self) -> dict: ...


def owner_key(owner: str) -> str:
    """The on-disk key for an owner (their token `sub`, or "anonymous" without auth)."""
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner is required")
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]


def new_file_id() -> str:
    return "f_" + secrets.token_urlsafe(16)


def valid_file_id(file_id: object) -> bool:
    return isinstance(file_id, str) and bool(_FILE_ID_RE.match(file_id))


def safe_name(name: object, fallback: str = "file") -> str:
    """A file name that is safe on disk, in a URL path segment and in Content-Disposition.

    Keeps the last path component only, drops control and format characters, quotes,
    backslashes and slashes, trims dots and spaces at the ends (no `..`, no hidden file) and
    cuts it to 200 UTF-8 bytes keeping the extension.
    """
    text = unicodedata.normalize("NFC", name if isinstance(name, str) else "")
    text = re.split(r"[\\/]", text)[-1]
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch)[0] != "C" and ch not in '"<>|*?:'
    )
    text = text.strip(" .")
    if not text:
        return fallback
    if len(text.encode("utf-8")) > _MAX_NAME_BYTES:
        stem, dot, ext = text.rpartition(".")
        if not dot or len(ext.encode("utf-8")) > 20:
            stem, ext = text, ""
        budget = _MAX_NAME_BYTES - (len(ext.encode("utf-8")) + 1 if ext else 0)
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .")
        text = f"{stem}.{ext}" if ext else stem
    return text or fallback


def safe_mime(mime: object) -> str:
    if isinstance(mime, str):
        base = mime.split(";", 1)[0].strip()
        if _MIME_RE.match(base):
            return base.lower()
    return "application/octet-stream"


class LocalStore:
    """`ResultStore` on a local directory."""

    def __init__(self, root: str | os.PathLike, *, default_ttl_sec: int = DEFAULT_TTL_SEC,
                 max_total_bytes: int = 2048 * 1024 * 1024,
                 max_owner_bytes: int = 512 * 1024 * 1024):
        if not 0 < default_ttl_sec <= MAX_TTL_SEC:
            raise ValueError(f"default TTL must be 1..{MAX_TTL_SEC} seconds")
        if max_total_bytes <= 0 or max_owner_bytes <= 0:
            raise ValueError("store caps must be positive")
        self.root = Path(root)
        self.default_ttl_sec = int(default_ttl_sec)
        self.max_total_bytes = int(max_total_bytes)
        self.max_owner_bytes = int(max_owner_bytes)
        self._lock = threading.RLock()
        self._index: dict[str, StoredFile] = {}
        self._reserved_total = 0
        self._reserved_owner: dict[str, int] = {}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._load()

    # ------------------------------------------------------------------ startup scan

    def _load(self) -> None:
        """Rebuild the index from disk, dropping anything that is not a complete live entry."""
        now = time.time()
        dropped = 0
        for owner_dir in list(self.root.iterdir()):
            if owner_dir.name.startswith("."):
                continue
            if not owner_dir.is_dir() or not _OWNER_KEY_RE.match(owner_dir.name):
                self._remove_path(owner_dir)
                dropped += 1
                continue
            for entry in list(owner_dir.iterdir()):
                meta = self._read_entry(owner_dir.name, entry)
                if meta is None or meta.expired(now):
                    self._remove_path(entry)
                    dropped += 1
                else:
                    self._index[meta.file_id] = meta
            self._remove_if_empty(owner_dir)
        if dropped:
            logger.info(f"File store: removed {dropped} expired or incomplete entr{'y' if dropped == 1 else 'ies'} at startup")
        logger.info(f"File store: {len(self._index)} file(s), {self._total_bytes()} bytes kept from the last run")

    def _read_entry(self, okey: str, entry: Path) -> StoredFile | None:
        if not entry.is_dir() or not valid_file_id(entry.name):
            return None
        try:
            raw = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
            meta = StoredFile(**raw)
            blob = entry / "blob"
            if (meta.file_id != entry.name or meta.owner_key != okey
                    or not blob.is_file() or blob.stat().st_size != meta.size):
                return None
            return meta
        except (OSError, ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ writes

    def put_bytes(self, owner: str, data: bytes, *, name: str, mime: str,
                  ttl_sec: int | None = None, kind: FileKind = "result") -> StoredFile:
        okey = owner_key(owner)
        size = len(data)
        self._reserve(okey, size)
        try:
            return self._write(okey, size, name, mime, ttl_sec, kind,
                               lambda f: f.write(data), hashlib.sha256(data).hexdigest())
        finally:
            self._release(okey, size)

    def put_file(self, owner: str, path: str | os.PathLike, *, name: str, mime: str,
                 ttl_sec: int | None = None, kind: FileKind = "result") -> StoredFile:
        okey = owner_key(owner)
        size = os.stat(path).st_size
        self._reserve(okey, size)
        digest = hashlib.sha256()

        def copy(f):
            with open(path, "rb") as src:
                while chunk := src.read(_COPY_CHUNK):
                    digest.update(chunk)
                    f.write(chunk)

        try:
            return self._write(okey, size, name, mime, ttl_sec, kind, copy, None, digest)
        finally:
            self._release(okey, size)

    def _ttl(self, ttl_sec: int | None) -> int:
        if ttl_sec is None:
            return self.default_ttl_sec
        if int(ttl_sec) <= 0:
            raise ValueError("ttl_sec must be positive")
        return min(int(ttl_sec), self.default_ttl_sec, MAX_TTL_SEC)

    def _write(self, okey, size, name, mime, ttl_sec, kind, fill, sha_hex, digest=None) -> StoredFile:
        ttl = self._ttl(ttl_sec)
        file_id = new_file_id()
        owner_dir = self.root / okey
        owner_dir.mkdir(exist_ok=True, mode=0o700)
        tmp = owner_dir / f"{file_id}.tmp"
        tmp.mkdir(mode=0o700)
        try:
            with open(tmp / "blob", "wb") as f:
                fill(f)
                f.flush()
                os.fsync(f.fileno())
            written = (tmp / "blob").stat().st_size
            if written != size:
                raise OSError(f"file changed while it was stored ({written} != {size} bytes)")
            now = time.time()
            meta = StoredFile(
                file_id=file_id, owner_key=okey, name=safe_name(name), mime=safe_mime(mime),
                size=size, sha256=sha_hex or digest.hexdigest(), created_at=now,
                expires_at=now + ttl, kind=kind,
            )
            (tmp / "meta.json").write_text(json.dumps(asdict(meta)), encoding="utf-8")
            os.replace(tmp, owner_dir / file_id)
        except BaseException:
            self._remove_path(tmp)
            raise
        with self._lock:
            self._index[file_id] = meta
        return meta

    def _reserve(self, okey: str, size: int) -> None:
        """Claim room for `size` bytes or raise StoreFullError (after one sweep of expired files)."""
        for cap, scope, whose in ((self.max_owner_bytes, "owner", "for one person"),
                                  (self.max_total_bytes, "total", "in total")):
            if size > cap:
                # Waiting for files to expire cannot help: say so instead of a retry time.
                raise StoreFullError(
                    f"The file ({size / (1024 * 1024):.1f} MB) is larger than the "
                    f"{cap // (1024 * 1024)} MB this server keeps {whose}, so it can never be "
                    f"stored.",
                    scope=scope, retry_after_sec=0, never_fits=True,
                )
        with self._lock:
            for attempt in (0, 1):
                total = self._total_bytes() + self._reserved_total
                mine = self._owner_bytes(okey) + self._reserved_owner.get(okey, 0)
                if total + size <= self.max_total_bytes and mine + size <= self.max_owner_bytes:
                    self._reserved_total += size
                    self._reserved_owner[okey] = self._reserved_owner.get(okey, 0) + size
                    return
                if attempt == 0:
                    self.sweep()
            retry = self._seconds_until_room(okey, size)
            if mine + size > self.max_owner_bytes:
                raise StoreFullError(
                    f"Your stored files would exceed {self.max_owner_bytes // (1024 * 1024)} MB "
                    f"(you have {mine / (1024 * 1024):.1f} MB in files that have not expired). "
                    f"Try again when older files expire.",
                    scope="owner", retry_after_sec=retry,
                )
            raise StoreFullError(
                f"The server's file store is full ({self.max_total_bytes // (1024 * 1024)} MB). "
                f"Try again when older files expire.",
                scope="total", retry_after_sec=retry,
            )

    def _release(self, okey: str, size: int) -> None:
        with self._lock:
            self._reserved_total -= size
            left = self._reserved_owner.get(okey, 0) - size
            if left > 0:
                self._reserved_owner[okey] = left
            else:
                self._reserved_owner.pop(okey, None)

    def _seconds_until_room(self, okey: str, size: int) -> int:
        """When enough files will have expired for the write to fit (an estimate for Retry-After)."""
        now = time.time()
        need_total = self._total_bytes() + self._reserved_total + size - self.max_total_bytes
        need_owner = (self._owner_bytes(okey) + self._reserved_owner.get(okey, 0)
                      + size - self.max_owner_bytes)
        when = now
        for need, files in ((need_total, self._index.values()),
                            (need_owner, [m for m in self._index.values() if m.owner_key == okey])):
            if need <= 0:
                continue
            freed = 0
            for m in sorted(files, key=lambda m: m.expires_at):
                freed += m.size
                if freed >= need:
                    when = max(when, m.expires_at)
                    break
            else:
                return self.default_ttl_sec
        return max(1, int(when - now) + 1)

    # ------------------------------------------------------------------ reads

    def get(self, file_id: str, owner: str | None = None) -> StoredFile | None:
        """The live file with this id; None when unknown, expired or (with `owner`) not theirs."""
        if not valid_file_id(file_id):
            return None
        with self._lock:
            meta = self._index.get(file_id)
        if meta is None or meta.expired():
            return None
        if owner is not None and meta.owner_key != owner_key(owner):
            return None
        return meta

    def blob_path(self, file_id: str) -> Path | None:
        meta = self.get(file_id)
        if meta is None:
            return None
        path = self.root / meta.owner_key / meta.file_id / "blob"
        return path if path.is_file() else None

    def open(self, file_id: str) -> BinaryIO:
        path = self.blob_path(file_id)
        if path is None:
            raise FileNotFoundError(file_id)
        return open(path, "rb")

    # ------------------------------------------------------------------ removal

    def delete(self, file_id: str, owner: str | None = None) -> bool:
        meta = self.get(file_id, owner)
        if meta is None:
            return False
        with self._lock:
            self._index.pop(file_id, None)
        self._remove_path(self.root / meta.owner_key / meta.file_id)
        self._remove_if_empty(self.root / meta.owner_key)
        return True

    def sweep(self, now: float | None = None) -> SweepResult:
        """Delete every expired file. Unexpired files are never touched."""
        now = time.time() if now is None else now
        with self._lock:
            gone = [m for m in self._index.values() if m.expired(now)]
            for m in gone:
                self._index.pop(m.file_id, None)
        for m in gone:
            self._remove_path(self.root / m.owner_key / m.file_id)
            self._remove_if_empty(self.root / m.owner_key)
        freed = sum(m.size for m in gone)
        if gone:
            logger.info(f"File store: swept {len(gone)} expired file(s), {freed} bytes")
        return SweepResult(removed=len(gone), freed_bytes=freed)

    def purge_all(self) -> int:
        """Delete every file (used when the links to them can no longer be verified)."""
        with self._lock:
            count = len(self._index)
            self._index.clear()
            for child in list(self.root.iterdir()):
                if not child.name.startswith("."):
                    self._remove_path(child)
        return count

    def has_entries_on_disk(self) -> bool:
        return any(not c.name.startswith(".") for c in self.root.iterdir())

    # ------------------------------------------------------------------ status

    def _total_bytes(self) -> int:
        return sum(m.size for m in self._index.values())

    def _owner_bytes(self, okey: str) -> int:
        return sum(m.size for m in self._index.values() if m.owner_key == okey)

    def stats(self) -> dict:
        """Counts and caps for /health. No names, ids or owners."""
        now = time.time()
        with self._lock:
            live = [m for m in self._index.values() if not m.expired(now)]
        return {
            "files": len(live),
            "bytes": sum(m.size for m in live),
            "owners": len({m.owner_key for m in live}),
            "max_bytes": self.max_total_bytes,
            "max_bytes_per_owner": self.max_owner_bytes,
            "ttl_sec": self.default_ttl_sec,
        }

    # ------------------------------------------------------------------ helpers

    def _remove_path(self, path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError as e:  # pragma: no cover - defensive
            logger.warning(f"File store: could not remove {path.name}: {e}")

    def _remove_if_empty(self, path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass
