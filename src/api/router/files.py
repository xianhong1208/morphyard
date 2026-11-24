"""Download endpoint for stored files: `GET /files/{file_id}/{name}?exp=…&sig=…` (issue #33).

The signature is the only credential (see src/domain/files/links.py). A bad or expired
signature, an unknown id and a file that has been swept all get the same 404, so the endpoint
tells nobody whether an id ever existed.

Query strings of these requests are never logged: uvicorn's access log gets a filter that cuts
them off, and the app's own request log records the path only.
"""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from src.domain.files import get_signer, get_store

router = APIRouter(tags=["Files"])

FILES_PREFIX = "/files/"

_NOT_FOUND = {
    "error": "not_found",
    "message": "This download link is not valid, has expired, or the file is no longer kept. "
               "Run the tool again to get a new link.",
}

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    # An HTML or SVG result opened in a browser never runs scripts or reaches the server's origin.
    "Content-Security-Policy": "sandbox",
}


def content_disposition(name: str) -> str:
    """`attachment` with an ASCII fallback and the exact UTF-8 name (RFC 6266 / 5987)."""
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\%' else "_" for ch in name)
    return f"attachment; filename=\"{fallback or 'file'}\"; filename*=UTF-8''{quote(name, safe='')}"


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=_NOT_FOUND, headers=_SECURITY_HEADERS)


@router.api_route("/files/{file_id}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
async def download(file_id: str, name: str, request: Request):
    store, signer = get_store(), get_signer()
    if store is None or signer is None:
        return _not_found()
    q = request.query_params
    if not signer.verify(file_id, name, q.get("exp"), q.get("sig")):
        return _not_found()
    meta = store.get(file_id)
    if meta is None or meta.name != name:
        return _not_found()
    path = store.blob_path(file_id)
    if path is None:
        return _not_found()
    headers = dict(_SECURITY_HEADERS)
    headers["Content-Disposition"] = content_disposition(meta.name)
    return FileResponse(path, media_type=meta.mime, headers=headers)


class _RedactFileQueries(logging.Filter):
    """Drop the query string (the signature) of /files/ requests from uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) \
                and args[2].startswith(FILES_PREFIX) and "?" in args[2]:
            record.args = args[:2] + (args[2].split("?", 1)[0] + "?…",) + args[3:]
        return True


def install_access_log_redaction() -> None:
    """Attach the filter to uvicorn's access logger (idempotent)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactFileQueries) for f in access.filters):
        access.addFilter(_RedactFileQueries())
