"""Turn a registered operation into an MCP tool.

An MCP tool here is a thin wrapper: its parameters are the operation's arguments
(taken from the operation's Pydantic model, so names, types, defaults and
descriptions live in one place) plus the input parameters (the uploaded file),
inserted where the tool has always had them. A call resolves the inputs, runs
the operation through `execute()` and shapes the reply the way this tool always
replied. tests/test_tool_contract.py pins the resulting tool list.

**Every tool that returns a list must be declared `output_schema=None`.**
FastMCP derives an outputSchema from a `-> list` annotation and then demands
structured output; but these tools return content blocks (text + file), not
structured data, so the client gets

    ToolError: Output validation error: outputSchema defined but no structured
    output returned

This error is invisible in unit tests: the tool function returns fine, and it
is FastMCP that rejects it while serializing the response. It only shows up
when a real MCP client makes the call. `add_operation_tool` sets it for every
reply style except REPORT.

**Result files are kept on the server and returned as links** (issue #33), as
`files.result_delivery` says (MORPHYARD_RESULT_DELIVERY):

    inline  [summary, file, ...]                       exactly as in 1.3, nothing stored
    auto    [summary + links, file (small ones only), ..., resource_link, ...]
    both    [summary + links, file, ..., resource_link, ...]
    link    [summary + links, resource_link, ...]

The embedded files are the same blocks 1.3 sent, byte for byte (hosts that turn
an inline file into an artifact keep working); the links come after them. The
URLs are also in the text because some clients ignore `resource_link`. Without a
file store (tests that build tools alone) the reply is the `inline` one.
"""

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.types import File
from mcp_types import ResourceLink
from pydantic import Field

from src.domain.convert.types import mime_type_for
from src.domain.files import KeptFile, NewFile, StoreFullError, get_runtime, keep_results
from src.domain.operations import (
    InputSource,
    OperationRefused,
    OpResult,
    execute,
    get_operation,
)
from src.domain.operations.sources import ANONYMOUS_OWNER, input_source

_MIME = {"zip": "application/zip"}


def output_mime(fmt: str) -> str:
    return _MIME.get(fmt) or mime_type_for(fmt)


class TypedFile(File):
    """A File labelled with the registered MIME type of its format.

    fastmcp's File derives the MIME type as "application/<format>" for every
    non-text format, so a Word document went out as "application/docx" and a PNG
    as "application/png". Overriding the public to_resource_content() (rather
    than File's private MIME attribute) keeps the URI and payload encoding
    exactly as File builds them.
    """

    def __init__(self, data: bytes, fmt: str, name: str):
        super().__init__(data=data, format=fmt, name=name)
        self._output_mime_type = output_mime(fmt)

    def to_resource_content(self, mime_type=None, annotations=None):
        return super().to_resource_content(
            mime_type=mime_type or self._output_mime_type, annotations=annotations,
        )


# ------------------------------------------------------------------------
# The upload triple
#
# The wording of these descriptions directly decides whether the service
# works. The host's file-injection mechanism works like this:
#
#     tool_args["file_content"] = b64
#     tool_args.setdefault("file_name", resolved_key)
#     if key != "file_content":
#         tool_args[key] = ""          # <- blanks the parameter that held the reference
#
# where key is whichever parameter the model put the reference into. So:
#
#   reference in file_content  -> key == "file_content", nothing is blanked,
#                                 setdefault fills the original name into
#                                 file_name (good)
#   reference in file_name     -> setdefault is a no-op because the key exists,
#                                 then file_name is blanked to "" (bad)
#
# The latter really happened in production logs
# (`Artifact resolved: (unnamed) -> format=pptx`), with two symptoms: the first
# call failed because the format could not be detected, and the returned file
# lost its original name. The old description said "the model need not fill
# this in", which pushed the model down exactly the wrong path. That is why the
# file_content description actively invites the model to put the reference there.
#
# Format detection no longer depends on the file name (detect_format inspects
# the container structure), but the name still decides what the returned file
# is called, so this guidance is still necessary.
#
# mime_type is never injected on the MCP path (only the host's REST-style
# path does that). The parameter is kept for compatibility with that path; do
# not remove it because it "looks unused".
# ------------------------------------------------------------------------

FILE_CONTENT = Annotated[str | None, Field(
    description="The file uploaded by the user. If you see a reference such as "
                "[Uploaded Artifact: \"xxx\"], put it in this parameter; the host "
                "replaces it with the actual file content automatically.")]
PDF_FILE_CONTENT = Annotated[str | None, Field(
    description="The PDF uploaded by the user. If you see a reference such as "
                "[Uploaded Artifact: \"xxx\"], put it in this parameter; the host "
                "replaces it with the actual file content automatically.")]
FILE_NAME = Annotated[str | None, Field(
    description="Original file name (with extension). Filled in by the host "
                "automatically; you do not need to set it. Never put an upload "
                "reference here.")]
MIME_TYPE = Annotated[str | None, Field(
    description="MIME type (optional, usually empty)")]
FILE_ID = Annotated[str | None, Field(
    description="Instead of file_content: the file_id of a file an earlier Morphyard "
                "tool returned (e.g. 'f_...'), to work on that result without passing "
                "it back. Give either file_content or file_id, not both. Never put an "
                "upload reference here; that goes in file_content.")]

_KW = inspect.Parameter.POSITIONAL_OR_KEYWORD


def current_owner() -> str:
    """Who the call's files belong to: the token's `sub`, or "anonymous" without auth.

    A token without a `sub` (MCP Gate always sets one) falls back to its client id.
    """
    token = get_access_token()
    if token is None:
        return ANONYMOUS_OWNER
    sub = (token.claims or {}).get("sub")
    if isinstance(sub, str) and sub:
        return sub
    return f"client:{token.client_id}"


@dataclass(frozen=True)
class Inputs:
    """The input parameters of a tool and how they become input sources."""

    params: list[inspect.Parameter]
    # Pops the input parameters from the call's kwargs; returns the sources.
    # Arguments: the kwargs, whether to detect the format, the caller (owner).
    sources: Callable[[dict[str, Any], bool, str], list[InputSource]]


def _upload_sources(kwargs: dict[str, Any], detect: bool, owner: str) -> list[InputSource]:
    return [input_source(
        owner=owner, detect=detect,
        file_content=kwargs.pop("file_content", None),
        file_name=kwargs.pop("file_name", None),
        mime_type=kwargs.pop("mime_type", None),
        file_id=kwargs.pop("file_id", None),
    )]


def upload(file_content=FILE_CONTENT) -> Inputs:
    """One input file: the file_content / file_name / mime_type triple, or a file_id."""
    return Inputs(
        params=[
            inspect.Parameter("file_content", _KW, default=None, annotation=file_content),
            inspect.Parameter("file_name", _KW, default=None, annotation=FILE_NAME),
            inspect.Parameter("mime_type", _KW, default=None, annotation=MIME_TYPE),
            inspect.Parameter("file_id", _KW, default=None, annotation=FILE_ID),
        ],
        sources=_upload_sources,
    )


# ------------------------------------------------------------------------
# Result files: stored, linked, and (depending on the delivery mode) inline
# ------------------------------------------------------------------------

def _size_text(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _mb_text(size: int) -> str:
    return f"{size / (1024 * 1024):g} MB"


def links_text(kept: list[KeptFile], attached: list[bool], mode: str,
               inline_max_bytes: int, now: float | None = None) -> str:
    """The part of the reply text that gives the links (appended to the summary)."""
    now = time.time() if now is None else now
    expires = min(k.meta.expires_at for k in kept)
    until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires))
    minutes = max(1, round((expires - now) / 60))
    plural = "s" if len(kept) > 1 else ""
    lines = [f"Download link{plural} (valid until {until}, about {minutes} min):"]
    for k, inline in zip(kept, attached):
        only = "" if inline else ", link only"
        lines.append(f"- {k.meta.name} ({_size_text(k.meta.size)}, file_id {k.meta.file_id}"
                     f"{only}): {k.url}")
    if not all(attached):
        if mode == "auto":
            lines.append(
                f"Files marked \"link only\" are larger than {_mb_text(inline_max_bytes)} and are "
                "not attached to this reply: they are available only at their URL, so give "
                "the user the link.")
        else:
            lines.append(
                f"The file{plural} {'are' if plural else 'is'} not attached to this reply: "
                f"{'they are' if plural else 'it is'} available only at the URL, so give the "
                "user the link.")
    lines.append("To use a result in another Morphyard tool, pass its file_id instead of "
                 "file_content.")
    return "\n".join(lines)


def resource_link(k: KeptFile) -> ResourceLink:
    until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(k.meta.expires_at))
    return ResourceLink(
        type="resource_link", uri=k.url, name=k.meta.name, mimeType=k.meta.mime,
        size=k.meta.size,
        description=f"Download link, valid until {until}; file_id {k.meta.file_id}",
    )


async def deliver_files(result: OpResult, owner: str) -> list:
    """[summary, file, ...] shaped by the delivery mode (see the module docstring)."""
    typed = [TypedFile(f.data, f.fmt, f.name) for f in result.files]
    runtime = get_runtime()
    mode = runtime.result_delivery if runtime is not None else "inline"
    if mode == "inline":
        return [result.summary, *typed]
    new = [NewFile(data=f.data, name=f"{f.name}.{f.fmt}", mime=output_mime(f.fmt))
           for f in result.files]
    try:
        kept = await asyncio.to_thread(keep_results, runtime.store, runtime.signer, owner, new)
    except StoreFullError as full:
        if full.details.get("never_fits"):
            advice = ("Nothing was stored. Tell the user; retrying will not succeed (the "
                      "operator can raise MORPHYARD_STORE_OWNER_MAX_MB / MORPHYARD_STORE_MAX_MB, "
                      "or a smaller result may fit).")
        else:
            advice = (f"Retry after about {full.details.get('retry_after_sec')} s. Nothing was "
                      "stored. Tell the user; retrying at once will not succeed.")
        raise ToolError(f"The result could not be kept on the server: {full.message} {advice}") from None
    if mode == "both":
        attached = [True] * len(kept)
    elif mode == "auto":
        attached = [len(f.data) <= runtime.inline_max_bytes for f in result.files]
    else:
        attached = [False] * len(kept)
    text = f"{result.summary}\n\n{links_text(kept, attached, mode, runtime.inline_max_bytes)}"
    return [text, *(t for t, a in zip(typed, attached) if a), *(resource_link(k) for k in kept)]


class Reply(Enum):
    """How a tool shapes its reply (each tool keeps the shape it always had).

    FILES          [summary, file, ...]; a refusal is [message].
    FILES_OR_TEXT  [summary, file, ...], or the summary alone as plain text when
                   there is no file; a refusal is the message as plain text.
                   (A list holding only strings would reach the client as one
                   JSON-encoded text block.)
    REPORT         the operation's report (dict or str); a refusal is
                   {"error": message}.
    """

    FILES = "files"
    FILES_OR_TEXT = "files_or_text"
    REPORT = "report"

    async def success(self, result: OpResult, owner: str = ANONYMOUS_OWNER):
        if self is Reply.REPORT:
            return result.report
        if not result.files:
            return result.summary if self is Reply.FILES_OR_TEXT else [result.summary]
        return await deliver_files(result, owner)

    def refusal(self, message: str):
        if self is Reply.REPORT:
            return {"error": message}
        if self is Reply.FILES_OR_TEXT:
            return message
        return [message]


def _arg_params(model) -> list[inspect.Parameter]:
    params = []
    for name, info in model.model_fields.items():
        annotation = Annotated[(info.annotation, *info.metadata,
                                Field(description=info.description))]
        default = inspect.Parameter.empty if info.is_required() else info.default
        params.append(inspect.Parameter(name, _KW, default=default, annotation=annotation))
    return params


def add_operation_tool(
    mcp: FastMCP,
    name: str,
    description: str,
    *,
    reply: Reply,
    returns: Any,
    inputs: Inputs | None = None,
    inputs_at: int | None = None,
    progress: bool = False,
):
    """Register the operation `name` as an MCP tool.

    description  the tool's docstring (what the model reads);
    reply        the reply shape (see Reply);
    returns      the return annotation (it decides the output schema of REPORT tools);
    inputs       the input parameters (None for a tool without input);
    inputs_at    where they go among the operation's arguments (default: last);
    progress     add a `ctx: Context` parameter and send the operation's
                 messages as MCP log notifications.
    """
    op = get_operation(name)
    args = _arg_params(op.args_model)
    position = len(args) if inputs_at is None else inputs_at
    params = args[:position] + (inputs.params if inputs else []) + args[position:]
    if progress:
        params.append(inspect.Parameter("ctx", _KW, default=None, annotation=Context))

    async def tool(**kwargs):
        ctx = kwargs.pop("ctx", None)
        owner = current_owner()
        sources = inputs.sources(kwargs, op.detect_input_format, owner) if inputs else []
        try:
            result = await execute(op, kwargs, sources,
                                   notify=ctx.info if ctx is not None else None)
        except OperationRefused as refused:
            return reply.refusal(refused.message)
        return await reply.success(result, owner)

    tool.__name__ = tool.__qualname__ = name
    tool.__doc__ = description
    tool.__signature__ = inspect.Signature(params, return_annotation=returns)
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": returns}
    if reply is Reply.REPORT:
        return mcp.tool()(tool)
    return mcp.tool(output_schema=None)(tool)
