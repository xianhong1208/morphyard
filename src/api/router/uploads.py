"""Upload endpoint: `PUT /uploads/{file_id}?exp=…&max=…&sig=…` (issue #36).

The URL comes from the `create_upload` tool and is the only credential (see
src/domain/files/uploads.py). The body is the file's raw bytes (`curl -T file URL`), plain or
chunked. Answers:

    201  stored: {file_id, name, size, sha256, expires_at}; the file_id now works as input
    400  empty body
    404  bad, tampered or expired URL, unknown id (one answer for all)
    409  the URL was used already, or an upload to it is in progress
    413  the body is larger than the URL allows (checked on Content-Length and while reading,
         so a wrong Content-Length or a chunked body cannot get past it)
    507  the owner's share of the store (or the whole store) has no room; Retry-After when
         waiting for older files to expire would help

A refused or broken-off upload leaves nothing behind and the URL can be tried again until it
expires. Query strings never reach the logs (files.py's access-log filter covers /uploads/).
"""

import asyncio
import mimetypes
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.domain.files import StoreFullError, UploadNotFound, UploadUsed, get_runtime
from src.domain.files.store import owner_key, safe_mime
from src.log import get_server_logger

logger = get_server_logger()

router = APIRouter(tags=["Files"])

_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff"}

_NOT_FOUND = {
    "error": "not_found",
    "message": "This upload link is not valid or has expired. Ask for a new one with the "
               "create_upload tool.",
}


def _answer(status: int, body: dict, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, headers={**_HEADERS, **(headers or {})})


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def _too_large(max_bytes: int) -> JSONResponse:
    return _answer(413, {
        "error": "too_large",
        "message": f"The file is larger than this upload link allows ({max_bytes} bytes, "
                   f"{_mb(max_bytes)}). Nothing was stored.",
        "max_bytes": max_bytes,
    })


def _mime(request: Request, name: str) -> str:
    sent = safe_mime(request.headers.get("content-type"))
    if sent != "application/octet-stream":
        return sent
    return safe_mime(mimetypes.guess_type(name)[0])


@router.put("/uploads/{file_id}", include_in_schema=False)
async def upload(file_id: str, request: Request):
    runtime = get_runtime()
    if runtime is None or runtime.uploads is None:
        return _answer(404, _NOT_FOUND)
    uploads, store = runtime.uploads, runtime.store
    q = request.query_params
    try:
        pending = uploads.claim(file_id, q.get("exp"), q.get("max"), q.get("sig"))
    except UploadNotFound:
        return _answer(404, _NOT_FOUND)
    except UploadUsed as used:
        return _answer(409, {
            "error": "upload_in_progress" if used.receiving else "already_used",
            "message": ("An upload to this link is in progress." if used.receiving else
                        "This upload link was used already; each link takes one file. The "
                        "file_id it gave is ready to use."),
        })

    stored = False
    part = uploads.part_path(pending)
    who = owner_key(pending.owner)[:8]
    try:
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > pending.max_bytes):
            return _too_large(pending.max_bytes)
        try:
            with store.reserve(pending.owner, pending.max_bytes):
                size = 0
                with open(part, "wb") as f:
                    os.chmod(part, 0o600)
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > pending.max_bytes:
                            return _too_large(pending.max_bytes)
                        f.write(chunk)
                if size == 0:
                    return _answer(400, {"error": "empty",
                                         "message": "The request had no body: send the file's "
                                                    "bytes, e.g. curl -T <file> '<url>'."})
                meta = await asyncio.to_thread(
                    store.put_file, pending.owner, part, name=pending.name,
                    mime=_mime(request, pending.name), kind="upload", file_id=pending.file_id,
                    reserved_bytes=pending.max_bytes,
                )
        except StoreFullError as full:
            retry = full.details.get("retry_after_sec") or 0
            return _answer(507, {"error": "storage_full", "message": full.message,
                                 "retry_after_sec": retry},
                           {"Retry-After": str(retry)} if retry else None)
        stored = True
        logger.info(f"[upload] {meta.file_id[:8]} stored for owner {who}: {meta.size} bytes, "
                    f"{meta.mime}")
        return _answer(201, {"file_id": meta.file_id, "name": meta.name, "size": meta.size,
                             "sha256": meta.sha256, "expires_at": int(meta.expires_at)})
    finally:
        uploads.finish(pending, stored)
        part.unlink(missing_ok=True)
        if not stored:
            logger.info(f"[upload] {pending.file_id[:8]} for owner {who}: not stored")
