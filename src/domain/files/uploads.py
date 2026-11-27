"""Signed, single-use upload URLs (issue #36).

`create_upload` (an MCP tool, so it runs with the caller's token) reserves a `file_id` for its
caller and hands out

    PUT {public_url}/uploads/{file_id}?exp=<unix seconds>&max=<bytes>&sig=<base64url>

(signed by `LinkSigner.upload_signature`). Whoever holds the URL may send the file's bytes as
the request body, once, before `exp`; the file is then kept in the store for the caller who
made the URL, under that `file_id`, and counts toward their share of the store. Nobody else
can use the `file_id` (the store checks the owner on every read).

The pending uploads live in memory: a restart forgets them (their URLs then get 404; the
caller asks for a new one). Bodies are received into `<data_dir>/uploads/<file_id>.part`,
which is deleted when the request ends whatever happens; the directory is emptied at startup
in case the process died mid-upload, and expired pending uploads are swept with the store.
"""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from src.domain.files.store import new_file_id, owner_key, safe_name
from src.log import get_server_logger

logger = get_server_logger()

# How long an upload URL can be used. Long enough for a person to run `curl -T`, short enough
# that a URL left in a chat log is soon worthless.
UPLOAD_TTL_SEC = 15 * 60

OPEN, RECEIVING, DONE = "open", "receiving", "done"


class UploadNotFound(Exception):
    """Bad or expired signature, unknown id: one answer for all (no oracle)."""


class UploadUsed(Exception):
    """The URL was used already (or is being used right now)."""

    def __init__(self, receiving: bool):
        super().__init__("receiving" if receiving else "done")
        self.receiving = receiving


@dataclass
class PendingUpload:
    file_id: str
    owner: str
    name: str
    max_bytes: int
    expires_at: float
    state: str = OPEN


class Uploads:
    """The pending uploads of one server process."""

    def __init__(self, parts_dir: str | Path, signer):
        self.parts_dir = Path(parts_dir)
        self._signer = signer
        self._lock = threading.Lock()
        self._pending: dict[str, PendingUpload] = {}
        # A part file is only ever alive during a request; anything here is a crash leftover.
        if self.parts_dir.exists():
            shutil.rmtree(self.parts_dir, ignore_errors=True)
        self.parts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def create(self, owner: str, name: str, max_bytes: int,
               now: float | None = None) -> tuple[PendingUpload, str]:
        """A new pending upload for `owner` and its signed URL."""
        owner_key(owner)  # refuses an empty owner
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        now = time.time() if now is None else now
        pending = PendingUpload(file_id=new_file_id(), owner=owner, name=safe_name(name),
                                max_bytes=int(max_bytes), expires_at=int(now) + UPLOAD_TTL_SEC)
        with self._lock:
            self._pending[pending.file_id] = pending
        url = self._signer.upload_url(pending.file_id, pending.expires_at, pending.max_bytes)
        return pending, url

    def claim(self, file_id: str, exp: object, max_bytes: object, sig: object,
              now: float | None = None) -> PendingUpload:
        """Start receiving: the URL must be valid, unexpired and not used yet. (The signature
        covers the id, expiry and size limit, so none of them can be changed.)"""
        if not self._signer.verify_upload(file_id, exp, max_bytes, sig, now=now):
            raise UploadNotFound()
        with self._lock:
            pending = self._pending.get(file_id)
            if pending is None:
                raise UploadNotFound()
            if pending.state != OPEN:
                raise UploadUsed(receiving=pending.state == RECEIVING)
            pending.state = RECEIVING
            return pending

    def finish(self, pending: PendingUpload, stored: bool) -> None:
        """Mark the upload used (stored) or open again (the body was refused or broke off:
        the same URL may be tried again until it expires)."""
        with self._lock:
            pending.state = DONE if stored else OPEN

    def part_path(self, pending: PendingUpload) -> Path:
        return self.parts_dir / f"{pending.file_id}.part"

    def sweep(self, now: float | None = None) -> int:
        """Forget expired uploads that are not being received. Returns how many."""
        now = time.time() if now is None else now
        with self._lock:
            gone = [fid for fid, p in self._pending.items()
                    if p.expires_at <= now and p.state != RECEIVING]
            for fid in gone:
                del self._pending[fid]
        for fid in gone:
            (self.parts_dir / f"{fid}.part").unlink(missing_ok=True)
        return len(gone)

    def stats(self) -> dict:
        with self._lock:
            return {"pending_uploads": sum(1 for p in self._pending.values() if p.state != DONE)}
