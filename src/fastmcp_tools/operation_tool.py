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
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import File
from pydantic import Field

from src.domain.convert.service import read_raw_upload, resolve_artifact_input
from src.domain.convert.types import mime_type_for, stem_of
from src.domain.operations import (
    Artifact,
    InputSource,
    OperationRefused,
    OpResult,
    execute,
    get_operation,
)

_MIME = {"zip": "application/zip"}


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
        self._output_mime_type = _MIME.get(fmt) or mime_type_for(fmt)

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

_KW = inspect.Parameter.POSITIONAL_OR_KEYWORD


@dataclass(frozen=True)
class Inputs:
    """The input parameters of a tool and how they become input sources."""

    params: list[inspect.Parameter]
    # Pops the input parameters from the call's kwargs; returns the sources.
    sources: Callable[[dict[str, Any], bool], list[InputSource]]


def _upload_sources(kwargs: dict[str, Any], detect: bool) -> list[InputSource]:
    file_content = kwargs.pop("file_content", None)
    file_name = kwargs.pop("file_name", None)
    mime_type = kwargs.pop("mime_type", None)

    def resolve() -> Artifact:
        if not detect:
            raw = read_raw_upload(file_content, file_name)
            return Artifact(data=raw, fmt=None, name=stem_of(file_name), file_name=file_name)
        source = resolve_artifact_input(file_content, file_name, mime_type)
        return Artifact(data=source.data, fmt=source.fmt, name=source.name,
                        file_name=file_name)
    return [resolve]


def upload(file_content=FILE_CONTENT) -> Inputs:
    """One uploaded file, as the file_content / file_name / mime_type triple."""
    return Inputs(
        params=[
            inspect.Parameter("file_content", _KW, default=None, annotation=file_content),
            inspect.Parameter("file_name", _KW, default=None, annotation=FILE_NAME),
            inspect.Parameter("mime_type", _KW, default=None, annotation=MIME_TYPE),
        ],
        sources=_upload_sources,
    )


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

    def success(self, result: OpResult):
        if self is Reply.REPORT:
            return result.report
        files = [TypedFile(f.data, f.fmt, f.name) for f in result.files]
        if self is Reply.FILES_OR_TEXT and not files:
            return result.summary
        return [result.summary, *files]

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
        sources = inputs.sources(kwargs, op.detect_input_format) if inputs else []
        try:
            result = await execute(op, kwargs, sources,
                                   notify=ctx.info if ctx is not None else None)
        except OperationRefused as refused:
            return reply.refusal(refused.message)
        return reply.success(result)

    tool.__name__ = tool.__qualname__ = name
    tool.__doc__ = description
    tool.__signature__ = inspect.Signature(params, return_annotation=returns)
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": returns}
    if reply is Reply.REPORT:
        return mcp.tool()(tool)
    return mcp.tool(output_schema=None)(tool)
