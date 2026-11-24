"""Files kept on the server and the links to download them (issue #33).

`init_files(config)` builds the store and the link signer once at app start;
`get_store()` / `get_signer()` return them; `files_lifespan()` runs the sweeper.
"""

from __future__ import annotations

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from src.domain.files.links import (
    LinkSigner,
    SigningKeyError,
    key_fingerprint,
    load_signing_key,
    reconcile_store_with_key,
)
from src.domain.files.store import (
    DEFAULT_TTL_SEC,
    MAX_TTL_SEC,
    LocalStore,
    ResultStore,
    StoredFile,
    StoreFullError,
    SweepResult,
    owner_key,
    safe_mime,
    safe_name,
    valid_file_id,
)
from src.log import get_server_logger

logger = get_server_logger()

_MB = 1024 * 1024


@dataclass(frozen=True)
class FilesRuntime:
    store: LocalStore
    signer: LinkSigner
    sweep_interval_sec: float
    result_delivery: str
    inline_max_bytes: int


_runtime: FilesRuntime | None = None


def resolve_public_url(config) -> str:
    """`files.public_url`, else `auth.base_url`, else http://<host>:<port>."""
    files = getattr(config, "files", None)
    url = (getattr(files, "public_url", None) or "").strip()
    if not url:
        url = (getattr(getattr(config, "auth", None), "base_url", None) or "").strip()
    if not url:
        server = config.server
        url = f"http://{server.host}:{server.port}"
    return url.rstrip("/")


def _is_loopback_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def init_files(config, env_refs: dict | None = None) -> FilesRuntime:
    """Build the store and signer from `config.files` (raises on a bad key or setting)."""
    global _runtime
    files = config.files
    if env_refs is not None and files.signing_key and "files.signing_key" not in env_refs:
        raise SigningKeyError(
            "files.signing_key must be an ${ENV_VAR} reference (MORPHYARD_SIGNING_KEY), "
            "not a literal value in config.yaml"
        )
    data_dir = Path(files.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key, source = load_signing_key(data_dir, files.signing_key or None)
    store = LocalStore(
        data_dir / "store",
        default_ttl_sec=files.ttl_sec,
        max_total_bytes=files.max_total_mb * _MB,
        max_owner_bytes=files.max_per_owner_mb * _MB,
    )
    reconcile_store_with_key(store, key)
    public_url = resolve_public_url(config)
    signer = LinkSigner(key, public_url)
    host = getattr(config.server, "host", "")
    if _is_loopback_url(public_url) and host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            f"Download links point at {public_url}, a loopback address, while the server listens on "
            f"{host}: people on other machines cannot open them. Set MORPHYARD_PUBLIC_URL to the "
            f"address they reach this server at."
        )
    logger.info(
        f"File store: {data_dir / 'store'} (keep {files.ttl_sec}s, {files.max_total_mb} MB total, "
        f"{files.max_per_owner_mb} MB per person); links at {public_url}/files/, key from {source} "
        f"(id {key_fingerprint(key)[:8]}); results delivered: {files.result_delivery}"
    )
    _runtime = FilesRuntime(
        store=store, signer=signer, sweep_interval_sec=files.sweep_interval_sec,
        result_delivery=files.result_delivery, inline_max_bytes=int(files.inline_max_mb * _MB),
    )
    return _runtime


def get_runtime() -> FilesRuntime | None:
    return _runtime


def set_runtime(runtime: FilesRuntime | None) -> None:
    global _runtime
    _runtime = runtime


def get_store() -> LocalStore | None:
    return _runtime.store if _runtime else None


def get_signer() -> LinkSigner | None:
    return _runtime.signer if _runtime else None


def files_status() -> dict:
    """The `store` block of /health."""
    if _runtime is None:
        return {"enabled": False}
    return {"enabled": True, **_runtime.store.stats()}


async def sweep_loop(store: LocalStore, interval_sec: float) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await asyncio.to_thread(store.sweep)
        except Exception as e:  # pragma: no cover - defensive: the loop must survive
            logger.warning(f"File store sweep failed: {e}")


@asynccontextmanager
async def files_lifespan():
    """Sweep at start, then every `sweep_interval_sec` until shutdown."""
    runtime = _runtime
    if runtime is None:
        yield
        return
    await asyncio.to_thread(runtime.store.sweep)
    task = asyncio.create_task(sweep_loop(runtime.store, runtime.sweep_interval_sec))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


__all__ = [
    "DEFAULT_TTL_SEC", "MAX_TTL_SEC", "FilesRuntime", "LinkSigner", "LocalStore", "ResultStore",
    "SigningKeyError", "StoreFullError", "StoredFile", "SweepResult", "files_lifespan",
    "files_status", "get_runtime", "get_signer", "get_store", "init_files", "owner_key",
    "resolve_public_url", "safe_mime", "safe_name", "set_runtime", "valid_file_id",
]
