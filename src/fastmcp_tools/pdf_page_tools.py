"""MCP tools for PDF pages: merge, delete, reorder, watermark, page numbers

Thin wrappers over the operations in src/domain/operations/pdf_page_ops.py,
registered from register_morphyard_tools(). The same contracts apply as in
morphyard_tools.py: the single input file arrives as the file_content /
file_name / mime_type triple, and every tool returns [summary, File].

pdf_merge is the exception to the triple. A host's file injection fills one
file_content per call, so merge takes a list of {file_content, file_name}
items whose base64 the caller supplies itself (any MCP client that holds the
files: Claude Code, FastMCP clients, scripts). Hosts that can only inject a
single upload cannot feed it yet; several uploaded files in one call is on the
1.4 roadmap.
"""

import inspect
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from src.domain.convert import pdf_pages
from src.domain.convert.service import resolve_artifact_input
from src.domain.operations import Artifact, InputSource
from src.fastmcp_tools.operation_tool import Inputs, Reply, add_operation_tool, upload
from src.log import get_mcptools_logger

logger = get_mcptools_logger()


class MergeInput(BaseModel):
    """One PDF to merge"""

    file_content: str = Field(description="The PDF itself, base64-encoded.")
    file_name: str | None = Field(
        default=None,
        description="Its file name, e.g. 'Chapter 1.pdf'. Used as the bookmark title for "
                    "this file in the merged PDF.")


def _merge_sources(kwargs: dict[str, Any], _detect: bool) -> list[InputSource]:
    """One source per item, read in order (the first unreadable item raises)."""

    def source(item: MergeInput) -> InputSource:
        def resolve() -> Artifact:
            src = resolve_artifact_input(item.file_content, item.file_name, None)
            return Artifact(data=src.data, fmt=src.fmt, name=src.name,
                            file_name=item.file_name)
        return resolve
    return [source(item) for item in kwargs.pop("files")]


MERGE_FILES = Inputs(
    params=[inspect.Parameter(
        "files", inspect.Parameter.POSITIONAL_OR_KEYWORD,
        annotation=Annotated[list[MergeInput], Field(
            min_length=2, max_length=pdf_pages.MAX_MERGE_FILES,
            description="The PDFs to merge, in the order they should appear: the first "
                        "item's pages come first. Each item carries one PDF as base64 in "
                        "file_content plus its file_name. "
                        f"2 to {pdf_pages.MAX_MERGE_FILES} files, at most "
                        f"{pdf_pages.MAX_MERGE_TOTAL_MB} MB together.")],
    )],
    sources=_merge_sources,
)


def register_pdf_page_tools(mcp: FastMCP):
    """Register pdf_merge, pdf_delete_pages, pdf_reorder_pages, pdf_watermark and
    pdf_page_numbers"""

    add_operation_tool(mcp, "pdf_merge", """Merge several PDFs into one PDF, in the given order.

        Pass every PDF yourself as base64 in `files` (this tool does not use the
        single file_content upload slot of the other tools). If you only hold an
        upload reference and cannot pass the bytes, tell the user merging needs the
        files themselves. Encrypted PDFs are refused: remove their password first.
        """, reply=Reply.FILES, returns=list, inputs=MERGE_FILES, inputs_at=0)

    add_operation_tool(mcp, "pdf_delete_pages", """Delete pages from a PDF and return the PDF without them.

        Bookmarks that pointed at a remaining page are kept. To keep only a few
        pages, pdf_extract_pages may be simpler; to change the order, use
        pdf_reorder_pages.
        """, reply=Reply.FILES, returns=list, inputs=upload())

    add_operation_tool(mcp, "pdf_reorder_pages", """Put the pages of a PDF in a new order (no page added or removed).

        Every page must appear exactly once; a missing or repeated page is refused
        with the page numbers named, so nothing is lost by accident. To also drop
        pages, call pdf_delete_pages first (or pdf_extract_pages, which keeps a
        selection in the order given). Bookmarks follow their pages.
        """, reply=Reply.FILES, returns=list, inputs=upload())

    add_operation_tool(mcp, "pdf_watermark", """Add a text or image watermark to a PDF, on every page or chosen pages.

        The watermark is drawn over the page content, semi-transparent, upright as
        the reader sees each page (also on landscape and rotated pages, and on
        documents that mix page sizes). Bookmarks, links and form fields are kept.
        """, reply=Reply.FILES, returns=list, inputs=upload())

    add_operation_tool(mcp, "pdf_page_numbers", """Add page numbers, a header and/or a footer to a PDF.

        Text is placed upright as the reader sees each page, inside the margin,
        on portrait, landscape and rotated pages alike, and is shrunk if it would
        not fit the page's width. The page number and a header or footer cannot
        share one spot (e.g. both bottom-center): move one, or put {page} into the
        footer_text and set number_format to an empty string.
        """, reply=Reply.FILES, returns=list, inputs=upload())

    logger.info("PDF page tools registered")
