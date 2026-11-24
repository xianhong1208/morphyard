"""Operations: merge, delete, reorder, watermark, page numbers (pdf_pages.py).

Delete and reorder are two operations rather than one "organize" operation with a
mode: each has one required argument with one meaning, so a model picks the right
tool from the user's words ("remove page 3" / "move the last page to the front"),
and reorder can insist that every page is listed exactly once without that rule
surprising someone who only wanted to delete.
"""

import base64
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain.convert import pdf_pages
from src.domain.convert.limiter import conversion_slot
from src.domain.operations.base import (
    Arity,
    Artifact,
    OpCall,
    Operation,
    OpResult,
    OutputFile,
    register,
)
from src.domain.operations.pdf_basic_ops import PDF, not_a_pdf

PAGES_HELP = ("1-based and inclusive, e.g. '1-3,7,10-' (pages 1 to 3, page 7, and page 10 "
              "through the last)")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


# ----------------------------------------------------------------------------
# pdf_merge
# ----------------------------------------------------------------------------

class PdfMergeArgs(_Args):
    output_name: Annotated[str | None, Field(
        description="Output file name without extension. Default: the first file's "
                    "name followed by '_merged'.")] = None
    bookmarks: Annotated[bool, Field(
        description="Add one top-level bookmark per input file, named after it, with "
                    "that file's own bookmarks nested underneath. Default true.")] = True


def _merge_wrong_input(artifact: Artifact, position: int) -> str:
    return (f"File #{position} ({artifact.file_name or 'unnamed'}) is not a PDF (detected "
            f"{artifact.fmt}). Convert it to PDF with convert_document first, then merge.")


async def _merge(call: OpCall) -> OpResult:
    args: PdfMergeArgs = call.args
    inputs: list[tuple[str, bytes]] = []
    for i, source in enumerate(call.inputs, start=1):
        label = (source.file_name or "").strip() or f"File {i}"
        if label.lower().endswith(".pdf"):
            label = label[:-4]
        inputs.append((label, source.data))
    pdf_pages.validate_merge_inputs(inputs)

    async with conversion_slot():
        data, pages = await pdf_pages.merge_pdfs(inputs, bookmarks=args.bookmarks)
    name = (args.output_name or "").strip() or f"{call.inputs[0].name}_merged"
    return OpResult(
        summary=f"Merged {len(inputs)} PDFs into one: {pages} pages, {_kb(len(data))}"
        + (", one bookmark per file" if args.bookmarks else ""),
        files=[OutputFile(data, "pdf", name)],
    )


register(Operation(
    name="pdf_merge",
    title="Merge several PDFs into one, in order",
    args_model=PdfMergeArgs,
    arity=Arity.MANY_TO_ONE,
    run=_merge,
    input_formats=PDF, output_formats=PDF, wrong_input=_merge_wrong_input,
))


# ----------------------------------------------------------------------------
# pdf_delete_pages
# ----------------------------------------------------------------------------

class PdfDeletePagesArgs(_Args):
    pages: Annotated[str, Field(
        description=f"The pages to remove, {PAGES_HELP}. At least one page must "
                    "remain.")]


def _prepare_delete(args: PdfDeletePagesArgs) -> None:
    if not args.pages or not args.pages.strip():
        raise pdf_pages.InvalidPdfOptionError(
            "pages is empty: say which pages to delete, e.g. '2,5-7'.")


async def _delete(call: OpCall) -> OpResult:
    source = call.inputs[0]
    async with conversion_slot():
        data, deleted, left = await pdf_pages.delete_pages(source.data, call.args.pages)
    return OpResult(
        summary=f"Deleted {len(deleted)} page(s) ({pdf_pages._compact(deleted)}); "
                f"{left} page(s) left, {_kb(len(data))}",
        files=[OutputFile(data, "pdf", f"{source.name}_pages_deleted")],
    )


register(Operation(
    name="pdf_delete_pages",
    title="Delete pages from a PDF",
    args_model=PdfDeletePagesArgs,
    arity=Arity.ONE_TO_ONE,
    run=_delete,
    prepare=_prepare_delete,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))


# ----------------------------------------------------------------------------
# pdf_reorder_pages
# ----------------------------------------------------------------------------

class PdfReorderPagesArgs(_Args):
    order: Annotated[str, Field(
        description="The complete new page order, 1-based, listing every page exactly "
                    "once: '3,1,2,4-10' puts page 3 first, then 1 and 2, then 4 to 10. "
                    "A descending range runs backwards ('10-1'), '5-' means page 5 to "
                    "the last, and 'reverse' turns the whole document around. Check the "
                    "page count with inspect_document first.")]


async def _reorder(call: OpCall) -> OpResult:
    source = call.inputs[0]
    async with conversion_slot():
        data, used = await pdf_pages.reorder_pages(source.data, call.args.order)
    shown = ", ".join(map(str, used[:30])) + (", ..." if len(used) > 30 else "")
    return OpResult(
        summary=f"Reordered {len(used)} pages; new order: {shown}. {_kb(len(data))}",
        files=[OutputFile(data, "pdf", f"{source.name}_reordered")],
    )


register(Operation(
    name="pdf_reorder_pages",
    title="Put the pages of a PDF in a new order",
    args_model=PdfReorderPagesArgs,
    arity=Arity.ONE_TO_ONE,
    run=_reorder,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))


# ----------------------------------------------------------------------------
# pdf_watermark
# ----------------------------------------------------------------------------

class PdfWatermarkArgs(_Args):
    text: Annotated[str | None, Field(
        description="Watermark text, e.g. 'CONFIDENTIAL' or '機密'. Chinese, Japanese "
                    f"and Korean work. At most {pdf_pages.MAX_TEXT_CHARS} characters. "
                    "Give this or watermark_image, not both.")] = None
    watermark_image: Annotated[str | None, Field(
        description="An image watermark instead of text: a PNG or JPEG as base64 (a "
                    "PNG with transparency keeps it). Pass the image bytes yourself; "
                    "the upload slot is file_content, which holds the PDF. At most "
                    f"{pdf_pages.MAX_WATERMARK_IMAGE_MB} MB.")] = None
    position: Annotated[Literal["center", "diagonal", "tiled"], Field(
        description="'center': one watermark in the middle, turned by `rotation`. "
                    "'diagonal': one watermark along the page's corner-to-corner "
                    "diagonal (ignores `rotation`). 'tiled': repeated across the whole "
                    "page, turned by `rotation`.")] = "diagonal"
    rotation: Annotated[float, Field(
        description="Angle in degrees, counter-clockwise, for 'center' and 'tiled'. "
                    "0 is horizontal.")] = 45
    font_size: Annotated[float, Field(
        description="Text size in points (6-300). A single watermark too long for the "
                    "page is shrunk to fit.")] = 48
    opacity: Annotated[float, Field(
        description="0.05 (barely visible) to 1 (solid). Default 0.3.")] = 0.3
    color: Annotated[str, Field(
        description="Text colour: hex such as '#808080' or a name such as 'gray', "
                    "'red', 'navy'.")] = "#808080"
    image_scale: Annotated[float, Field(
        description="Image watermark size as a fraction of the page (0.05-1): the image "
                    "fits within this share of the page's width and height.")] = 0.5
    pages: Annotated[str, Field(
        description=f"Pages to watermark, {PAGES_HELP}. Empty means every page.")] = ""


def _prepare_watermark(a: PdfWatermarkArgs) -> pdf_pages.WatermarkOptions:
    image = None
    if a.watermark_image and a.watermark_image.strip():
        try:
            blob = base64.b64decode(a.watermark_image.strip(), validate=False)
        except Exception:
            raise pdf_pages.InvalidPdfOptionError("watermark_image is not valid base64.")
        image = pdf_pages.load_watermark_image(blob)
    opts = pdf_pages.WatermarkOptions(
        text=a.text or "", image=image, position=a.position, rotation=a.rotation,
        font_size=a.font_size, opacity=a.opacity, color=a.color, image_scale=a.image_scale,
        pages=a.pages,
    )
    pdf_pages.validate_watermark(opts)
    return opts


async def _watermark(call: OpCall) -> OpResult:
    source, a, opts = call.inputs[0], call.args, call.prepared
    async with conversion_slot():
        data, stamped = await pdf_pages.watermark_pdf(source.data, opts)
    kind = f"a text watermark {a.text!r}" if opts.image is None else "an image watermark"
    return OpResult(
        summary=f"Added {kind} ({a.position}) to {stamped} page(s), {_kb(len(data))}",
        files=[OutputFile(data, "pdf", f"{source.name}_watermarked")],
    )


register(Operation(
    name="pdf_watermark",
    title="Add a text or image watermark to a PDF",
    args_model=PdfWatermarkArgs,
    arity=Arity.ONE_TO_ONE,
    run=_watermark,
    prepare=_prepare_watermark,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))


# ----------------------------------------------------------------------------
# pdf_page_numbers
# ----------------------------------------------------------------------------

class PdfPageNumbersArgs(_Args):
    number_format: Annotated[str, Field(
        description="Page number text with the placeholders {page} and {total}, e.g. "
                    "'{page}', '{page} / {total}', 'Page {page} of {total}', "
                    "'第 {page} 頁'. Empty string: no page numbers (header/footer "
                    "only).")] = "{page}"
    number_position: Annotated[Literal[
        "bottom-center", "bottom-left", "bottom-right",
        "top-center", "top-left", "top-right",
    ], Field(description="Where the page number goes.")] = "bottom-center"
    start_number: Annotated[int, Field(
        description="The number shown on the first numbered page.")] = 1
    skip_first_page: Annotated[bool, Field(
        description="Leave the first page (e.g. a cover) without number, header or "
                    "footer; numbering then starts on page 2 with start_number.")] = False
    pages: Annotated[str, Field(
        description=f"Pages to stamp, {PAGES_HELP}. Empty means every page. Only "
                    "stamped pages are counted: {page} runs start_number, "
                    "start_number+1, ... over them and {total} is the last number "
                    "shown.")] = ""
    header_text: Annotated[str, Field(
        description="Text at the top of each stamped page, e.g. a document title. "
                    "May use {page} and {total}. Chinese works. Empty: no header.")] = ""
    header_align: Annotated[Literal["left", "center", "right"], Field(
        description="Header alignment.")] = "center"
    footer_text: Annotated[str, Field(
        description="Text at the bottom of each stamped page. May use {page} and "
                    "{total}. Empty: no footer.")] = ""
    footer_align: Annotated[Literal["left", "center", "right"], Field(
        description="Footer alignment. Must not be the page number's spot.")] = "left"
    font_size: Annotated[float, Field(
        description="Text size in points (6-72).")] = 10
    margin: Annotated[float, Field(
        description="Distance from the page edge in points (72 points = 1 inch, "
                    "28 = 1 cm), 0-200.")] = 28
    color: Annotated[str, Field(
        description="Text colour: hex such as '#000000' or a name such as 'gray'.")] \
        = "#000000"


def _prepare_page_numbers(a: PdfPageNumbersArgs) -> pdf_pages.PageNumberOptions:
    opts = pdf_pages.PageNumberOptions(
        number_format=a.number_format, number_position=a.number_position,
        start_number=a.start_number, skip_first_page=a.skip_first_page, pages=a.pages,
        header_text=a.header_text, header_align=a.header_align,
        footer_text=a.footer_text, footer_align=a.footer_align,
        font_size=a.font_size, margin=a.margin, color=a.color,
    )
    pdf_pages.validate_page_numbers(opts)
    return opts


async def _page_numbers(call: OpCall) -> OpResult:
    source, a = call.inputs[0], call.args
    async with conversion_slot():
        data, stamped = await pdf_pages.add_page_numbers(source.data, call.prepared)
    added = [what for what, value in (("page numbers", a.number_format),
                                      ("a header", a.header_text),
                                      ("a footer", a.footer_text)) if value.strip()]
    listed = added[0] if len(added) == 1 else ", ".join(added[:-1]) + " and " + added[-1]
    return OpResult(
        summary=f"Added {listed} to {stamped} page(s), {_kb(len(data))}",
        files=[OutputFile(data, "pdf", f"{source.name}_numbered")],
    )


register(Operation(
    name="pdf_page_numbers",
    title="Add page numbers, a header and/or a footer to a PDF",
    args_model=PdfPageNumbersArgs,
    arity=Arity.ONE_TO_ONE,
    run=_page_numbers,
    prepare=_prepare_page_numbers,
    input_formats=PDF, output_formats=PDF, wrong_input=not_a_pdf,
))
