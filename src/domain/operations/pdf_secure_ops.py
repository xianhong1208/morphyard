"""Operations: compress, protect, unlock, read and strip metadata (pikepdf, pdf_secure.py).

pdf_metadata only reads, like inspect_document, and takes no conversion slot.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from src.domain.convert import pdf_secure
from src.domain.convert.limiter import conversion_slot
from src.domain.operations.base import (
    Arity,
    OpCall,
    Operation,
    OpResult,
    OutputFile,
    register,
)
from src.domain.operations.pdf_basic_ops import PDF, not_a_pdf

_PASSWORD_NOTE = (
    "Passwords given to this tool appear in the conversation history; suggest the user "
    "does not reuse a password from another system."
)


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------
# pdf_compress
# ----------------------------------------------------------------------------

class PdfCompressArgs(_Args):
    level: Annotated[str, Field(
        description="'lossless': restructure only (object streams, recompressed "
                    "streams, unused objects removed); pages look identical. "
                    "'balanced' (default): also downsample images above 150 DPI and "
                    "re-encode them as JPEG quality 75; good for sharing and email. "
                    "'small': images to 96 DPI, JPEG quality 50; smallest, visibly "
                    "softer images.")] = "balanced"
    max_dpi: Annotated[int, Field(
        description="Optional override of the level's image resolution (36-600). "
                    "0 = the level's default. Not allowed with 'lossless'.")] = 0
    jpeg_quality: Annotated[int, Field(
        description="Optional override of the level's JPEG quality (10-95). "
                    "0 = the level's default. Not allowed with 'lossless'.")] = 0


async def _compress(call: OpCall) -> OpResult:
    source, args = call.inputs[0], call.args
    async with conversion_slot():
        r = await pdf_secure.compress_pdf(source.data, args.level, args.max_dpi or None,
                                          args.jpeg_quality or None)
    if r.returned_original:
        summary = (
            f"Not compressed: the '{r.level}' result was not meaningfully smaller than "
            f"the original ({_kb(r.original_bytes)}), so the original file is returned "
            "unchanged. Tell the user the PDF is already compact"
            + {"lossless": "; 'balanced' or 'small' can still shrink its images.",
               "balanced": "; 'small' may shrink it further at lower image quality."}
            .get(r.level, ".")
        )
        name = source.name
    else:
        saved = 100 * (1 - r.output_bytes / r.original_bytes)
        summary = (
            f"Compressed ({r.level}): {_kb(r.original_bytes)} -> {_kb(r.output_bytes)} "
            f"({saved:.0f}% smaller). Images downsampled: {r.images_downsampled}, "
            f"re-encoded: {r.images_recompressed}, left as they were: {r.images_skipped}."
        )
        name = f"{source.name}_compressed"
    if r.notes:
        summary += " Note: " + "; ".join(r.notes) + "."
    return OpResult(summary=summary, files=[OutputFile(r.data, "pdf", name)])


register(Operation(
    name="pdf_compress",
    title="Make a PDF smaller (lossless restructure, or downsampled images)",
    args_model=PdfCompressArgs,
    arity=Arity.ONE_TO_ONE,
    run=_compress,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))


# ----------------------------------------------------------------------------
# pdf_protect
# ----------------------------------------------------------------------------

class PdfProtectArgs(_Args):
    password: Annotated[str, Field(
        description="Open password: needed to open the PDF at all. Leave empty for "
                    "a PDF anyone can open but whose permissions are restricted.")] = ""
    owner_password: Annotated[str, Field(
        description="Owner password: lifts the restrictions later. Required when "
                    "any allow_* is false, and must differ from the open password. "
                    "Leave empty when only setting an open password.")] = ""
    allow_print: Annotated[bool, Field(description="Readers may print")] = True
    allow_copy: Annotated[bool, Field(
        description="Readers may copy text and images (screen readers keep "
                    "access either way)")] = True
    allow_modify: Annotated[bool, Field(
        description="Readers may edit the document and insert/delete/rotate "
                    "pages")] = True
    allow_annotate: Annotated[bool, Field(
        description="Readers may add comments and fill forms")] = True
    allow_fill_forms: Annotated[bool, Field(
        description="Readers may fill in form fields")] = True


async def _protect(call: OpCall) -> OpResult:
    source, a = call.inputs[0], call.args
    allow = {"print": a.allow_print, "copy": a.allow_copy, "modify": a.allow_modify,
             "annotate": a.allow_annotate, "fill_forms": a.allow_fill_forms}
    async with conversion_slot():
        data, info = await pdf_secure.protect_pdf(source.data, a.password,
                                                   a.owner_password, allow)
    parts = ["Encrypted with AES-256."]
    parts.append("An open password is required to open it." if info["open_password"]
                 else "Anyone can open it.")
    if info["restricted"]:
        parts.append("Not allowed without the owner password: "
                     + ", ".join(k.replace("_", " ") for k in info["restricted"]) + ".")
    parts.append(_PASSWORD_NOTE)
    return OpResult(summary=" ".join(parts),
                    files=[OutputFile(data, "pdf", f"{source.name}_protected")])


register(Operation(
    name="pdf_protect",
    title="Encrypt a PDF (AES-256): open password and/or permission restrictions",
    args_model=PdfProtectArgs,
    arity=Arity.ONE_TO_ONE,
    run=_protect,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))


# ----------------------------------------------------------------------------
# pdf_unlock
# ----------------------------------------------------------------------------

class PdfUnlockArgs(_Args):
    password: Annotated[str, Field(
        description="The PDF's password, as given by the user. The owner password "
                    "always works; the open password works when the owner set no "
                    "permission restrictions.")]


async def _unlock(call: OpCall) -> OpResult:
    source = call.inputs[0]
    async with conversion_slot():
        data = await pdf_secure.unlock_pdf(source.data, call.args.password)
    if data is None:
        # Nothing to do: a note and no file
        return OpResult(summary="This PDF is not encrypted; there is nothing to unlock.")
    return OpResult(
        summary="Unlocked: the returned copy has no password and no permission restrictions.",
        files=[OutputFile(data, "pdf", f"{source.name}_unlocked")],
    )


register(Operation(
    name="pdf_unlock",
    title="Remove a PDF's password and restrictions, given its password",
    args_model=PdfUnlockArgs,
    arity=Arity.ONE_TO_ONE,
    run=_unlock,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))


# ----------------------------------------------------------------------------
# pdf_metadata
# ----------------------------------------------------------------------------

class PdfMetadataArgs(_Args):
    password: Annotated[str, Field(
        description="Only for a PDF that needs a password to open; otherwise "
                    "leave empty")] = ""


async def _metadata(call: OpCall) -> OpResult:
    return OpResult(report=await pdf_secure.read_metadata(call.inputs[0].data,
                                                          call.args.password))


register(Operation(
    name="pdf_metadata",
    title="Report a PDF's metadata without changing it",
    args_model=PdfMetadataArgs,
    arity=Arity.ONE_TO_REPORT,
    run=_metadata,
    input_formats=PDF, wrong_input=not_a_pdf,
    takes_slot=False,
))


# ----------------------------------------------------------------------------
# pdf_strip_metadata
# ----------------------------------------------------------------------------

class PdfStripMetadataArgs(_Args):
    remove_annotation_authors: Annotated[bool, Field(
        description="Remove author names and dates from comments/annotations "
                    "(the comments themselves stay)")] = True
    remove_javascript: Annotated[bool, Field(
        description="Also remove document JavaScript and actions (open actions, "
                    "page/field actions). May break interactive forms that "
                    "calculate")] = False
    remove_attachments: Annotated[bool, Field(
        description="Also remove embedded files and file-attachment "
                    "annotations")] = False


async def _strip(call: OpCall) -> OpResult:
    source, a = call.inputs[0], call.args
    async with conversion_slot():
        r = await pdf_secure.strip_metadata(source.data, a.remove_annotation_authors,
                                            a.remove_javascript, a.remove_attachments)
    done = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in r.removed.items() if v)
    summary = ("Metadata removed (" + (done or "there was nothing to remove") + "). "
               f"Size {_kb(len(source.data))} -> {_kb(len(r.data))}.")
    return OpResult(summary=summary, files=[OutputFile(r.data, "pdf", f"{source.name}_clean")])


register(Operation(
    name="pdf_strip_metadata",
    title="A copy of a PDF without its metadata",
    args_model=PdfStripMetadataArgs,
    arity=Arity.ONE_TO_ONE,
    run=_strip,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))
