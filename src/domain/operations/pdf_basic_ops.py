"""Operations: extract, split and rotate PDF pages (pypdf, pdfops.py)."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from src.domain.convert import pdfops
from src.domain.convert.limiter import conversion_slot
from src.domain.operations.base import (
    Arity,
    Artifact,
    OpCall,
    Operation,
    OperationRefused,
    OpResult,
    OutputFile,
    register,
)

PDF = frozenset({"pdf"})

# Split refuses to hand back more files than this; pdf_extract_pages takes a range.
MAX_SPLIT_FILES = 50


def not_a_pdf(artifact: Artifact, _position: int = 1) -> str:
    """The refusal most PDF operations give for another format."""
    return (f"This is not a PDF (detected {artifact.fmt}). "
            "Convert it to PDF with convert_document first.")


def _not_a_pdf_short(artifact: Artifact, _position: int = 1) -> str:
    return f"This is not a PDF (detected {artifact.fmt})."


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------
# pdf_extract_pages
# ----------------------------------------------------------------------------

class PdfExtractPagesArgs(_Args):
    pages: Annotated[str, Field(
        description="Page range, 1-based and inclusive. E.g. '1-3,7,10-' means "
                    "pages 1 to 3, page 7, and page 10 through the last")]


async def _extract_pages(call: OpCall) -> OpResult:
    source, pages = call.inputs[0], call.args.pages
    async with conversion_slot():
        data = await pdfops.extract_pages(source.data, pages)
    name = f"{source.name}_p{pages.replace(',', '_').replace('-', 'to')}"
    return OpResult(summary=f"Extracted pages {pages}, {len(data) / 1024:.1f} KB",
                    files=[OutputFile(data, "pdf", name)])


register(Operation(
    name="pdf_extract_pages",
    title="Take chosen pages of a PDF into a new PDF, in the order given",
    args_model=PdfExtractPagesArgs,
    arity=Arity.ONE_TO_ONE,
    run=_extract_pages,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))


# ----------------------------------------------------------------------------
# pdf_split
# ----------------------------------------------------------------------------

class PdfSplitArgs(_Args):
    pages_per_file: Annotated[int, Field(
        description="Pages per output file. 1 means one file per page")] = 1


async def _split(call: OpCall) -> OpResult:
    source = call.inputs[0]
    async with conversion_slot():
        chunks = await pdfops.split_pdf(source.data, call.args.pages_per_file)
    if len(chunks) > MAX_SPLIT_FILES:
        raise OperationRefused(
            f"This would produce {len(chunks)} files, which is too many. "
            f"Increase pages_per_file, or use pdf_extract_pages to take only the "
            f"pages you need."
        )
    return OpResult(
        summary=f"Split into {len(chunks)} files",
        files=[OutputFile(blob, "pdf", f"{source.name}_{label}") for label, blob in chunks],
    )


register(Operation(
    name="pdf_split",
    title="Split a PDF into files of N pages",
    args_model=PdfSplitArgs,
    arity=Arity.ONE_TO_MANY,
    run=_split,
    input_formats=PDF, output_formats=PDF, wrong_input=_not_a_pdf_short,
))


# ----------------------------------------------------------------------------
# pdf_rotate
# ----------------------------------------------------------------------------

class PdfRotateArgs(_Args):
    degrees: Annotated[int, Field(
        description="Rotation angle, must be a multiple of 90 (90/180/270)")]
    pages: Annotated[str, Field(
        description="Page range to rotate, e.g. '1-3'. Empty means all pages")] = ""


async def _rotate(call: OpCall) -> OpResult:
    source, args = call.inputs[0], call.args
    async with conversion_slot():
        data = await pdfops.rotate_pages(source.data, args.degrees, args.pages)
    scope = args.pages or "all pages"
    return OpResult(summary=f"Rotated {scope} by {args.degrees} degrees",
                    files=[OutputFile(data, "pdf", f"{source.name}_rotated")])


register(Operation(
    name="pdf_rotate",
    title="Rotate pages of a PDF by a multiple of 90 degrees",
    args_model=PdfRotateArgs,
    arity=Arity.ONE_TO_ONE,
    run=_rotate,
    input_formats=PDF, output_formats=PDF, wrong_input=_not_a_pdf_short,
))
