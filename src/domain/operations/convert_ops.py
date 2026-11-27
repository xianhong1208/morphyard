"""Operations: capability listing, inspection, conversion, text extraction."""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from src.domain.convert import pdfops
from src.domain.convert.registry import (
    FIDELITY_LOSSY,
    available_engines,
    capability_matrix,
    format_path,
)
from src.domain.convert.service import check_target_format, convert_source
from src.domain.convert.types import FALLBACK_NAME, SourceFile
from src.domain.operations.base import (
    Arity,
    OpCall,
    Operation,
    OpResult,
    OutputFile,
    register,
)
from src.log import get_mcptools_logger

logger = get_mcptools_logger()


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _source(call: OpCall) -> SourceFile:
    a = call.inputs[0]
    return SourceFile(data=a.data, fmt=a.fmt, name=a.name)


# ----------------------------------------------------------------------------
# list_supported_conversions
# ----------------------------------------------------------------------------

class ListSupportedConversionsArgs(_Args):
    source_format: Annotated[str | None, Field(
        description="Set this to see only what one source format can be converted "
                    "to, e.g. 'docx'")] = None


async def _list_supported_conversions(call: OpCall) -> OpResult:
    args: ListSupportedConversionsArgs = call.args
    matrix = capability_matrix()
    if args.source_format:
        key = args.source_format.strip().lower().lstrip(".")
        matrix = {key: matrix.get(key, [])}

    return OpResult(report={
        "supported": matrix,
        "engines": available_engines(),
        "note": (
            "This table lists single-step direct conversions only. Unlisted pairs "
            "(e.g. md -> pdf) may still be possible via an intermediate format; "
            "just call convert_document, which reports a clear error if the "
            "conversion is not feasible."
        ),
    })


register(Operation(
    name="list_supported_conversions",
    title="List the conversion pairs the installed engines support",
    args_model=ListSupportedConversionsArgs,
    arity=Arity.NONE_TO_REPORT,
    run=_list_supported_conversions,
    takes_slot=False,
))


# ----------------------------------------------------------------------------
# inspect_document
# ----------------------------------------------------------------------------

class InspectDocumentArgs(_Args):
    pass


async def _inspect_document(call: OpCall) -> OpResult:
    source = call.inputs[0]
    info: dict[str, Any] = {
        "detected_format": source.fmt,
        "size_mb": round(len(source.data) / (1024 * 1024), 3),
        "file_name": source.file_name,
        "can_convert_to": capability_matrix().get(source.fmt, []),
    }
    if source.fmt == "pdf":
        try:
            info["page_count"] = await pdfops.page_count(source.data)
        except Exception as e:
            info["page_count_error"] = str(e)
    return OpResult(report=info)


register(Operation(
    name="inspect_document",
    title="Detected format, size, page count and possible targets of a file",
    args_model=InspectDocumentArgs,
    arity=Arity.ONE_TO_REPORT,
    run=_inspect_document,
    takes_slot=False,
))


# ----------------------------------------------------------------------------
# convert_document
# ----------------------------------------------------------------------------

def _log_delivered(name: str, fmt: str, size: int, source_name: str | None) -> None:
    """Log the file name the user will actually download

    Without this line, "is the file name right" can only be learned from user
    reports: server logs look perfectly normal up to `Artifact resolved` and
    never show what the delivered file was called. Falling back to the default
    name gets an extra warning, because it means the original name was lost
    somewhere.
    """
    delivered = f"{name}.{fmt}"
    if name == FALLBACK_NAME:
        logger.warning(
            f"[DELIVER] {delivered} ({size / 1024:.1f} KB) -- "
            f"fallback file name used. The original name did not arrive "
            f"(file_name={source_name!r}) and the model gave no output_name, so the "
            f"user receives a file with an unrecognizable name."
        )
    else:
        logger.info(f"[DELIVER] {delivered} ({size / 1024:.1f} KB)")


def fidelity_warning(plan) -> str:
    """Phrase the quality loss along the path as a warning for the model

    Once the model has the result it must decide what to tell the user. If a
    lossy engine was involved, this text lets it know it should warn the user
    that the layout has changed, instead of reporting a bare "conversion
    succeeded".
    """
    if not plan:
        return ""
    lossy = [r for r in plan if r.fidelity == FIDELITY_LOSSY]
    if lossy:
        # The wording must be precise about what survived and what did not.
        # The old text said "images have been lost", which stopped being true
        # once image extraction was added; the model relayed it, and users
        # assumed the images were gone without looking at the end of the
        # document. A wrong warning misleads more than no warning.
        return (
            "\n⚠️ This conversion went through content extraction. Proactively tell "
            "the user two things: (1) text and images are preserved, but **layout, "
            "fonts and table styling have been re-flowed** and need manual "
            "adjustment; (2) the original images are collected in an **\"Images "
            "from the original document\" section at the end** of the output, "
            "labelled by page, not at their original paragraph positions. The PDF "
            "does not record the relative placement of text and images, so it "
            "cannot be restored."
        )
    if len(plan) > 1:
        return (
            "\nNote: this conversion went through an intermediate format; fine "
            "styling details may differ slightly from the original."
        )
    return ""


class ConvertDocumentArgs(_Args):
    target_format: Annotated[str, Field(
        description="Target format, e.g. pdf / docx / md / xlsx / png. If unsure "
                    "whether it is supported, call list_supported_conversions first")]
    output_name: Annotated[str, Field(
        description="Output file name without extension. Use the stem of the "
                    "user's original file name: if the upload is "
                    "\"Vendor Review 2026.pptx\", pass \"Vendor Review 2026\" so the "
                    "user recognizes the download. If the user asked for a new "
                    "name, use that instead.")]
    allow_quality_loss: Annotated[bool, Field(
        description="Allow layout quality to be sacrificed so the conversion can "
                    "succeed; allowed by default. The response carries an explicit "
                    "quality warning that you must relay to the user. Set to False "
                    "only when the user explicitly says the layout must not be "
                    "lost, even at the cost of not converting")] = True


def _prepare_convert(args: ConvertDocumentArgs) -> str:
    # target_format is judged before the file is read (service.convert_artifact)
    return check_target_format(args.target_format)


async def _convert_document(call: OpCall) -> OpResult:
    args: ConvertDocumentArgs = call.args
    output, plan = await convert_source(
        _source(call), call.prepared,
        prefer_fidelity=not args.allow_quality_loss,
        output_name=args.output_name,
    )

    path_desc = format_path(plan) if plan else "no conversion needed"
    engines_used = ", ".join(r.engine for r in plan) if plan else "-"
    summary = (
        f"Conversion complete: {output.name}.{output.fmt}\n"
        f"Path: {path_desc} (engines: {engines_used})\n"
        f"Size: {len(output.data) / 1024:.1f} KB"
        f"{fidelity_warning(plan)}"
    )

    await call.say(f"Done: {len(output.data)} bytes")

    _log_delivered(output.name, output.fmt, len(output.data), call.inputs[0].file_name)
    return OpResult(summary=summary,
                    files=[OutputFile(output.data, output.fmt, output.name)])


register(Operation(
    name="convert_document",
    title="Convert a file to another format",
    args_model=ConvertDocumentArgs,
    arity=Arity.ONE_TO_ONE,
    run=_convert_document,
    prepare=_prepare_convert,
    output_formats=None,   # chosen by target_format
    outputs_for=lambda _args, target: frozenset({target}),
    start_message=lambda args: f"Converting to {args.target_format}...",
))


# ----------------------------------------------------------------------------
# extract_text
# ----------------------------------------------------------------------------

class ExtractTextArgs(_Args):
    max_chars: Annotated[int, Field(
        description="Maximum number of characters returned; longer text is "
                    "truncated. 0 means no limit")] = 50000


async def _extract_text(call: OpCall) -> OpResult:
    args: ExtractTextArgs = call.args
    output, _ = await convert_source(
        _source(call), "md",
        prefer_fidelity=False,
        # Explicitly disable image embedding. md counts as an image-carrying
        # format and would get data: URIs by default, but this operation's
        # result is text bound for the model's context: one 500KB image would
        # become 680,000 characters of base64.
        embed_images=False,
    )
    text = output.data.decode("utf-8", errors="replace")
    if args.max_chars and len(text) > args.max_chars:
        omitted = len(text) - args.max_chars
        text = text[:args.max_chars] + f"\n\n... (truncated, {omitted} characters omitted)"
    return OpResult(report=text)


register(Operation(
    name="extract_text",
    title="A document's text as Markdown",
    args_model=ExtractTextArgs,
    arity=Arity.ONE_TO_REPORT,
    run=_extract_text,
    output_formats=frozenset({"md"}),
))
