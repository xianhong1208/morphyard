"""MCP tools for PDF pages: merge, delete, reorder, watermark, page numbers

Registered from register_morphyard_tools(). The same contracts apply as in
morphyard_tools.py: the single input file arrives as the file_content /
file_name / mime_type triple, and every tool returns [summary, File], so each
one is declared `@mcp.tool(output_schema=None)`.

pdf_merge is the exception to the triple. A host's file injection fills one
file_content per call, so merge takes a list of {file_content, file_name}
items whose base64 the caller supplies itself (any MCP client that holds the
files: Claude Code, FastMCP clients, scripts). Hosts that can only inject a
single upload cannot feed it yet; several uploaded files in one call is on the
1.4 roadmap.

Delete and reorder are two tools rather than one "organize" tool with a mode:
each has one required argument with one meaning, so a model picks the right
tool from the user's words ("remove page 3" / "move the last page to the
front"), and reorder can insist that every page is listed exactly once without
that rule surprising someone who only wanted to delete.
"""

import base64
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.utilities.types import File
from pydantic import BaseModel, Field

from src.domain.convert import pdf_pages
from src.domain.convert.limiter import conversion_slot
from src.domain.convert.service import resolve_artifact_input
from src.domain.convert.types import stem_of
from src.fastmcp_tools.morphyard_tools import _FILE_CONTENT, _FILE_NAME, _MIME_TYPE
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

_PAGES_HELP = ("1-based and inclusive, e.g. '1-3,7,10-' (pages 1 to 3, page 7, and page 10 "
               "through the last)")


class MergeInput(BaseModel):
    """One PDF to merge"""

    file_content: str = Field(description="The PDF itself, base64-encoded.")
    file_name: str | None = Field(
        default=None,
        description="Its file name, e.g. 'Chapter 1.pdf'. Used as the bookmark title for "
                    "this file in the merged PDF.")


def _not_a_pdf(fmt: str) -> list:
    return [f"This is not a PDF (detected {fmt}). Convert it to PDF with convert_document "
            "first."]


def _pdf_source(file_content, file_name, mime_type):
    source = resolve_artifact_input(file_content, file_name, mime_type)
    return source, (None if source.fmt == "pdf" else _not_a_pdf(source.fmt))


def register_pdf_page_tools(mcp: FastMCP):
    """Register pdf_merge, pdf_delete_pages, pdf_reorder_pages, pdf_watermark and
    pdf_page_numbers"""

    @mcp.tool(output_schema=None)
    async def pdf_merge(
        files: Annotated[list[MergeInput], Field(
            min_length=2, max_length=pdf_pages.MAX_MERGE_FILES,
            description="The PDFs to merge, in the order they should appear: the first "
                        "item's pages come first. Each item carries one PDF as base64 in "
                        "file_content plus its file_name. "
                        f"2 to {pdf_pages.MAX_MERGE_FILES} files, at most "
                        f"{pdf_pages.MAX_MERGE_TOTAL_MB} MB together.")],
        output_name: Annotated[str | None, Field(
            description="Output file name without extension. Default: the first file's "
                        "name followed by '_merged'.")] = None,
        bookmarks: Annotated[bool, Field(
            description="Add one top-level bookmark per input file, named after it, with "
                        "that file's own bookmarks nested underneath. Default true.")] = True,
    ) -> list:
        """Merge several PDFs into one PDF, in the given order.

        Pass every PDF yourself as base64 in `files` (this tool does not use the
        single file_content upload slot of the other tools). If you only hold an
        upload reference and cannot pass the bytes, tell the user merging needs the
        files themselves. Encrypted PDFs are refused: remove their password first.
        """
        inputs: list[tuple[str, bytes]] = []
        for i, item in enumerate(files, start=1):
            source = resolve_artifact_input(item.file_content, item.file_name, None)
            if source.fmt != "pdf":
                return [f"File #{i} ({item.file_name or 'unnamed'}) is not a PDF (detected "
                        f"{source.fmt}). Convert it to PDF with convert_document first, "
                        "then merge."]
            label = (item.file_name or "").strip() or f"File {i}"
            if label.lower().endswith(".pdf"):
                label = label[:-4]
            inputs.append((label, source.data))
        pdf_pages.validate_merge_inputs(inputs)

        async with conversion_slot():
            data, pages = await pdf_pages.merge_pdfs(inputs, bookmarks=bookmarks)
        name = (output_name or "").strip() or f"{stem_of(files[0].file_name)}_merged"
        return [
            f"Merged {len(inputs)} PDFs into one: {pages} pages, {len(data) / 1024:.1f} KB"
            + (", one bookmark per file" if bookmarks else ""),
            File(data=data, format="pdf", name=name),
        ]

    @mcp.tool(output_schema=None)
    async def pdf_delete_pages(
        pages: Annotated[str, Field(
            description=f"The pages to remove, {_PAGES_HELP}. At least one page must "
                        "remain.")],
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Delete pages from a PDF and return the PDF without them.

        Bookmarks that pointed at a remaining page are kept. To keep only a few
        pages, pdf_extract_pages may be simpler; to change the order, use
        pdf_reorder_pages.
        """
        if not pages or not pages.strip():
            raise pdf_pages.InvalidPdfOptionError(
                "pages is empty: say which pages to delete, e.g. '2,5-7'.")
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return refusal
        async with conversion_slot():
            data, deleted, left = await pdf_pages.delete_pages(source.data, pages)
        return [
            f"Deleted {len(deleted)} page(s) ({pdf_pages._compact(deleted)}); "
            f"{left} page(s) left, {len(data) / 1024:.1f} KB",
            File(data=data, format="pdf", name=f"{source.name}_pages_deleted"),
        ]

    @mcp.tool(output_schema=None)
    async def pdf_reorder_pages(
        order: Annotated[str, Field(
            description="The complete new page order, 1-based, listing every page exactly "
                        "once: '3,1,2,4-10' puts page 3 first, then 1 and 2, then 4 to 10. "
                        "A descending range runs backwards ('10-1'), '5-' means page 5 to "
                        "the last, and 'reverse' turns the whole document around. Check the "
                        "page count with inspect_document first.")],
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Put the pages of a PDF in a new order (no page added or removed).

        Every page must appear exactly once; a missing or repeated page is refused
        with the page numbers named, so nothing is lost by accident. To also drop
        pages, call pdf_delete_pages first (or pdf_extract_pages, which keeps a
        selection in the order given). Bookmarks follow their pages.
        """
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return refusal
        async with conversion_slot():
            data, used = await pdf_pages.reorder_pages(source.data, order)
        shown = ", ".join(map(str, used[:30])) + (", ..." if len(used) > 30 else "")
        return [
            f"Reordered {len(used)} pages; new order: {shown}. {len(data) / 1024:.1f} KB",
            File(data=data, format="pdf", name=f"{source.name}_reordered"),
        ]

    @mcp.tool(output_schema=None)
    async def pdf_watermark(
        text: Annotated[str | None, Field(
            description="Watermark text, e.g. 'CONFIDENTIAL' or '機密'. Chinese, Japanese "
                        f"and Korean work. At most {pdf_pages.MAX_TEXT_CHARS} characters. "
                        "Give this or watermark_image, not both.")] = None,
        watermark_image: Annotated[str | None, Field(
            description="An image watermark instead of text: a PNG or JPEG as base64 (a "
                        "PNG with transparency keeps it). Pass the image bytes yourself; "
                        "the upload slot is file_content, which holds the PDF. At most "
                        f"{pdf_pages.MAX_WATERMARK_IMAGE_MB} MB.")] = None,
        position: Annotated[Literal["center", "diagonal", "tiled"], Field(
            description="'center': one watermark in the middle, turned by `rotation`. "
                        "'diagonal': one watermark along the page's corner-to-corner "
                        "diagonal (ignores `rotation`). 'tiled': repeated across the whole "
                        "page, turned by `rotation`.")] = "diagonal",
        rotation: Annotated[float, Field(
            description="Angle in degrees, counter-clockwise, for 'center' and 'tiled'. "
                        "0 is horizontal.")] = 45,
        font_size: Annotated[float, Field(
            description="Text size in points (6-300). A single watermark too long for the "
                        "page is shrunk to fit.")] = 48,
        opacity: Annotated[float, Field(
            description="0.05 (barely visible) to 1 (solid). Default 0.3.")] = 0.3,
        color: Annotated[str, Field(
            description="Text colour: hex such as '#808080' or a name such as 'gray', "
                        "'red', 'navy'.")] = "#808080",
        image_scale: Annotated[float, Field(
            description="Image watermark size as a fraction of the page (0.05-1): the image "
                        "fits within this share of the page's width and height.")] = 0.5,
        pages: Annotated[str, Field(
            description=f"Pages to watermark, {_PAGES_HELP}. Empty means every page.")] = "",
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Add a text or image watermark to a PDF, on every page or chosen pages.

        The watermark is drawn over the page content, semi-transparent, upright as
        the reader sees each page (also on landscape and rotated pages, and on
        documents that mix page sizes). Bookmarks, links and form fields are kept.
        """
        image = None
        if watermark_image and watermark_image.strip():
            try:
                blob = base64.b64decode(watermark_image.strip(), validate=False)
            except Exception:
                raise pdf_pages.InvalidPdfOptionError("watermark_image is not valid base64.")
            image = pdf_pages.load_watermark_image(blob)
        opts = pdf_pages.WatermarkOptions(
            text=text or "", image=image, position=position, rotation=rotation,
            font_size=font_size, opacity=opacity, color=color, image_scale=image_scale,
            pages=pages,
        )
        pdf_pages.validate_watermark(opts)
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return refusal
        async with conversion_slot():
            data, stamped = await pdf_pages.watermark_pdf(source.data, opts)
        kind = f"a text watermark {text!r}" if image is None else "an image watermark"
        return [
            f"Added {kind} ({position}) to {stamped} page(s), {len(data) / 1024:.1f} KB",
            File(data=data, format="pdf", name=f"{source.name}_watermarked"),
        ]

    @mcp.tool(output_schema=None)
    async def pdf_page_numbers(
        number_format: Annotated[str, Field(
            description="Page number text with the placeholders {page} and {total}, e.g. "
                        "'{page}', '{page} / {total}', 'Page {page} of {total}', "
                        "'第 {page} 頁'. Empty string: no page numbers (header/footer "
                        "only).")] = "{page}",
        number_position: Annotated[Literal[
            "bottom-center", "bottom-left", "bottom-right",
            "top-center", "top-left", "top-right",
        ], Field(description="Where the page number goes.")] = "bottom-center",
        start_number: Annotated[int, Field(
            description="The number shown on the first numbered page.")] = 1,
        skip_first_page: Annotated[bool, Field(
            description="Leave the first page (e.g. a cover) without number, header or "
                        "footer; numbering then starts on page 2 with start_number.")] = False,
        pages: Annotated[str, Field(
            description=f"Pages to stamp, {_PAGES_HELP}. Empty means every page. Only "
                        "stamped pages are counted: {page} runs start_number, "
                        "start_number+1, ... over them and {total} is the last number "
                        "shown.")] = "",
        header_text: Annotated[str, Field(
            description="Text at the top of each stamped page, e.g. a document title. "
                        "May use {page} and {total}. Chinese works. Empty: no header.")] = "",
        header_align: Annotated[Literal["left", "center", "right"], Field(
            description="Header alignment.")] = "center",
        footer_text: Annotated[str, Field(
            description="Text at the bottom of each stamped page. May use {page} and "
                        "{total}. Empty: no footer.")] = "",
        footer_align: Annotated[Literal["left", "center", "right"], Field(
            description="Footer alignment. Must not be the page number's spot.")] = "left",
        font_size: Annotated[float, Field(
            description="Text size in points (6-72).")] = 10,
        margin: Annotated[float, Field(
            description="Distance from the page edge in points (72 points = 1 inch, "
                        "28 = 1 cm), 0-200.")] = 28,
        color: Annotated[str, Field(
            description="Text colour: hex such as '#000000' or a name such as 'gray'.")]
            = "#000000",
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Add page numbers, a header and/or a footer to a PDF.

        Text is placed upright as the reader sees each page, inside the margin,
        on portrait, landscape and rotated pages alike, and is shrunk if it would
        not fit the page's width. The page number and a header or footer cannot
        share one spot (e.g. both bottom-center): move one, or put {page} into the
        footer_text and set number_format to an empty string.
        """
        opts = pdf_pages.PageNumberOptions(
            number_format=number_format, number_position=number_position,
            start_number=start_number, skip_first_page=skip_first_page, pages=pages,
            header_text=header_text, header_align=header_align,
            footer_text=footer_text, footer_align=footer_align,
            font_size=font_size, margin=margin, color=color,
        )
        pdf_pages.validate_page_numbers(opts)
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return refusal
        async with conversion_slot():
            data, stamped = await pdf_pages.add_page_numbers(source.data, opts)
        added = [what for what, value in (("page numbers", number_format),
                                          ("a header", header_text),
                                          ("a footer", footer_text)) if value.strip()]
        listed = added[0] if len(added) == 1 else ", ".join(added[:-1]) + " and " + added[-1]
        return [
            f"Added {listed} to {stamped} page(s), {len(data) / 1024:.1f} KB",
            File(data=data, format="pdf", name=f"{source.name}_numbered"),
        ]

    logger.info("PDF page tools registered")
