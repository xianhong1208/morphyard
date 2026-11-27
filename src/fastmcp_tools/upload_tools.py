"""store_file and create_upload: getting files onto the server first (issue #36).

Two ways for a host to give Morphyard several files without the model copying base64 around:

* `store_file` takes one file through the usual file_content / file_name / mime_type triple
  (the parameters a host's file injection fills) and keeps it as it is, answering with its
  `file_id`. A host that injects one upload per call stores each upload with one call and
  the model then passes the `file_id`s to pdf_merge / images_to_pdf in `files`.
* `create_upload` hands out a signed, single-use URL (src/domain/files/uploads.py) that a
  client, script or person can `PUT` the bytes to (`curl -T`). Its `file_id` is fixed when the
  URL is made and works as input once the upload is done.

Both keep the file for the caller (the token's `sub`) only, for the store's TTL, and count it
toward the caller's share of the store.
"""

import asyncio
import time
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from src.domain.convert import service
from src.domain.convert.types import mime_type_for
from src.domain.files import UPLOAD_TTL_SEC, StoreFullError, get_runtime, safe_mime, safe_name
from src.fastmcp_tools.operation_tool import (
    FILE_CONTENT,
    FILE_NAME,
    MIME_TYPE,
    _size_text,
    _store_full_error,
    current_owner,
)
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

_MB = 1024 * 1024

NO_STORE = ("This server keeps no files (its file store is not set up), so files cannot be "
            "stored ahead. Pass each file in file_content instead.")


def _until(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _stored_name(file_name: str | None, fmt: str | None) -> str:
    name = (file_name or "").strip()
    if name:
        return safe_name(name)
    return f"file.{fmt}" if fmt else "file"


def register_upload_tools(mcp: FastMCP) -> None:
    @mcp.tool(output_schema=None)
    async def store_file(
        file_content: FILE_CONTENT = None,
        file_name: FILE_NAME = None,
        mime_type: MIME_TYPE = None,
    ) -> str:
        """Keep one uploaded file on the server and get its file_id, without converting it.

        Use it to give a Morphyard tool several uploaded files when each upload can only
        be passed in a single file_content: call store_file once per file (put the upload
        reference, e.g. [Uploaded Artifact: "a.pdf"], in file_content), collect the
        file_ids, then pass them all at once, e.g. pdf_merge or images_to_pdf with
        files=[{"file_id": "f_..."}, {"file_id": "f_..."}]. Any tool also takes a single
        file_id instead of file_content. The file is kept as it is, for you only, for a
        limited time (the reply says until when).
        """
        runtime = get_runtime()
        if runtime is None:
            raise ToolError(NO_STORE)
        owner = current_owner()
        raw, fmt = service.decode_upload_to_store(file_content, file_name, mime_type)
        name = _stored_name(file_name, fmt)
        mime = mime_type_for(fmt) if fmt else safe_mime(mime_type)
        try:
            meta = await asyncio.to_thread(runtime.store.put_bytes, owner, raw, name=name,
                                           mime=mime, kind="upload")
        except StoreFullError as full:
            raise _store_full_error(full) from None
        logger.info(f"[store_file] {meta.file_id[:8]} {meta.size} bytes, "
                    f"{fmt or 'unrecognised format'}")
        kind = f"a {fmt.upper()} file" if fmt else "a file of a format not recognised here"
        return (f"Stored \"{meta.name}\" ({kind}, {_size_text(meta.size)}) as file_id "
                f"{meta.file_id}, kept until {_until(meta.expires_at)}.\n"
                f"Pass it to a Morphyard tool as file_id=\"{meta.file_id}\", or as "
                f"{{\"file_id\": \"{meta.file_id}\"}} in the files list of pdf_merge or "
                "images_to_pdf. Store each further file the same way, then make one call "
                "with all the file_ids in order.")

    @mcp.tool(output_schema=None)
    async def create_upload(
        file_name: Annotated[str, Field(
            description="The name of the file that will be uploaded, with extension, e.g. "
                        "'scan 1.pdf'. The stored file keeps this name.")],
        size_bytes: Annotated[int | None, Field(
            description="The file's size in bytes, if known: the upload URL then accepts "
                        "at most that many bytes. Default: the server's input limit.",
            gt=0)] = None,
    ) -> str:
        """Get a single-use URL to upload one file to, and the file_id it will have.

        For clients, scripts and people that can send an HTTP request (e.g. `curl -T
        <file> '<url>'`): sending the file's raw bytes with PUT to the URL stores it,
        and the file_id then works as input to any Morphyard tool (file_id, or an item
        of files in pdf_merge / images_to_pdf), with no base64 through the conversation.
        The URL works once and expires after 15 minutes. If you only hold an upload
        reference such as [Uploaded Artifact: "a.pdf"], use store_file instead.
        """
        runtime = get_runtime()
        if runtime is None or runtime.uploads is None:
            raise ToolError(NO_STORE)
        limit = int(service.MAX_INPUT_MB * _MB)
        if size_bytes is not None and size_bytes > limit:
            raise ToolError(
                f"The file ({_size_text(size_bytes)}) is larger than the {service.MAX_INPUT_MB} "
                "MB this server takes as input. Tell the user; a smaller file (e.g. a page "
                "range of a PDF) may fit.")
        max_bytes = size_bytes or limit
        pending, url = runtime.uploads.create(current_owner(), file_name, max_bytes)
        logger.info(f"[create_upload] {pending.file_id[:8]} up to {max_bytes} bytes")
        return (
            f"Upload URL for \"{pending.name}\" (single use, valid until "
            f"{_until(pending.expires_at)}, about {UPLOAD_TTL_SEC // 60} min; at most "
            f"{_size_text(max_bytes)}):\n{url}\n\n"
            f"Send the file's bytes as the body of a PUT request to it, for example:\n"
            f"  curl -T <path to the file> '{url}'\n"
            f"When the upload has finished (HTTP 201), the file has file_id {pending.file_id}: "
            f"pass it to a Morphyard tool as file_id, or in files as "
            f"{{\"file_id\": \"{pending.file_id}\"}}. Until then the file_id is not usable.")

    logger.info("Upload tools registered")
