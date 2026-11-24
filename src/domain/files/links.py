"""Signed, expiring download links (issue #33).

    GET {public_url}/files/{file_id}/{name}?exp=<unix seconds>&sig=<base64url>
    sig = HMAC-SHA256(key, "v1|GET|{file_id}|{exp}|{name}")

Anyone holding the link may download the file until `exp` (a capability URL, like S3 presigned
URLs): the owner check happens when the link is made, because only the owner's tool call gets
one. The link stops working earlier if the file is gone from the store.

The signing key is `MORPHYARD_SIGNING_KEY` when set, otherwise 32 random bytes generated once into
`<data_dir>/.signing_key` (mode 600). The store records a fingerprint of the key its links were
made with (`<store>/.key_id`). If that fingerprint is missing or differs while the store holds
files - the key file was deleted, the variable changed, or the fingerprint itself was lost - the
links to those files can no longer be verified, so the store is purged with a warning instead of
quietly serving under a new key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import quote

from src.log import get_server_logger

logger = get_server_logger()

_KEY_FILE = ".signing_key"
_KEY_ID_FILE = ".key_id"
MIN_ENV_KEY_CHARS = 32


class SigningKeyError(RuntimeError):
    """The signing key cannot be used; the server refuses to start."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class LinkSigner:
    """Makes and checks download links."""

    def __init__(self, key: bytes, public_url: str):
        if len(key) < 32:
            raise ValueError("signing key must be at least 32 bytes")
        self._key = key
        self.public_url = public_url.rstrip("/")

    @property
    def key_id(self) -> str:
        return key_fingerprint(self._key)

    def signature(self, file_id: str, exp: int, name: str) -> str:
        msg = f"v1|GET|{file_id}|{int(exp)}|{name}".encode("utf-8")
        return _b64(hmac.new(self._key, msg, hashlib.sha256).digest())

    def url(self, file_id: str, name: str, exp: float) -> str:
        exp = int(exp)
        sig = self.signature(file_id, exp, name)
        return (f"{self.public_url}/files/{file_id}/{quote(name, safe='')}"
                f"?exp={exp}&sig={sig}")

    def verify(self, file_id: str, name: str, exp: object, sig: object,
               now: float | None = None) -> bool:
        """True when `sig` is this server's signature for the link and `exp` has not passed."""
        if not isinstance(sig, str) or not isinstance(exp, str) or not exp.isdigit() or len(exp) > 12:
            return False
        expected = self.signature(file_id, int(exp), name)
        if not hmac.compare_digest(expected.encode("ascii"), sig.encode("utf-8", "replace")):
            return False
        return int(exp) > (time.time() if now is None else now)


def key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(b"morphyard-signing-key-id|" + key).hexdigest()[:16]


def load_signing_key(data_dir: str | os.PathLike, env_key: str | None) -> tuple[bytes, str]:
    """Return (key, source). Generates the key file on first use; refuses an unusable key."""
    if env_key:
        if len(env_key) < MIN_ENV_KEY_CHARS:
            raise SigningKeyError(
                f"MORPHYARD_SIGNING_KEY is too short: use at least {MIN_ENV_KEY_CHARS} random "
                f"characters (for example `openssl rand -hex 32`), or unset it to let the server "
                f"generate one into the data directory."
            )
        return env_key.encode("utf-8"), "MORPHYARD_SIGNING_KEY"

    path = Path(data_dir) / _KEY_FILE
    if path.exists():
        try:
            raw = path.read_text(encoding="ascii").strip()
            key = bytes.fromhex(raw)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raise SigningKeyError(f"{path} is not a valid signing key ({e}); "
                                  f"delete it to have a new one generated (existing links stop working)") from e
        if len(key) < 32:
            raise SigningKeyError(f"{path} holds a key shorter than 32 bytes; "
                                  f"delete it to have a new one generated (existing links stop working)")
        return key, str(path)

    key = os.urandom(32)
    Path(data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key.hex() + "\n")
    logger.info(f"Generated a new download-link signing key in {path}")
    return key, str(path)


def reconcile_store_with_key(store, key: bytes) -> int:
    """Purge the store when its files were linked with another (or an unknown) key.

    Returns how many files were purged. Then records the current key's fingerprint.
    """
    id_path = Path(store.root) / _KEY_ID_FILE
    current = key_fingerprint(key)
    try:
        recorded = id_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        recorded = None
    purged = 0
    if recorded != current and store.has_entries_on_disk():
        purged = store.purge_all()
        reason = ("the signing key changed" if recorded
                  else "the store has no record of the key its links were signed with")
        logger.warning(
            f"File store: {reason}, so the links to the {purged} stored file(s) can no longer be "
            f"verified; the files were deleted. Keep the data directory's .signing_key (or "
            f"MORPHYARD_SIGNING_KEY) together with the store to keep links working across restarts."
        )
    if recorded != current:
        tmp = id_path.with_suffix(".tmp")
        tmp.write_text(current + "\n", encoding="ascii")
        os.replace(tmp, id_path)
    return purged
