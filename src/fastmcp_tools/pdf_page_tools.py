"""MCP tools for PDF pages: merge, delete, reorder, watermark, page numbers

Thin wrappers over the operations in src/domain/operations/pdf_page_ops.py,
registered from register_morphyard_tools(). The same contracts apply as in
morphyard_tools.py: the single input file arrives as the file_content /
file_name / mime_type triple, and every tool returns [summary, File].

pdf_merge is the exception to the triple: it takes `files`, a list of FileInput items
(file_inputs.py), each the PDF itself (base64) or the file_id of a PDF kept on the server. A
host that injects an upload into one file_content per call gets several uploads there by
storing each with store_file first and passing the file_ids.
"""

from typing import Any

from fastmcp import FastMCP

from src.domain.convert import pdf_pages
from src.domain.operations import InputSource
from src.fastmcp_tools.file_inputs import files_parameter, item_sources
from src.fastmcp_tools.operation_tool import Inputs, Reply, add_operation_tool, upload
from src.log import get_mcptools_logger

logger = get_mcptools_logger()


def _merge_sources(kwargs: dict[str, Any], detect: bool, owner: str) -> list[InputSource]:
    """One source per item, read in order (the first unreadable item raises)."""
    return item_sources(kwargs.pop("files"), owner, detect)


MERGE_FILES = Inputs(
    params=[files_parameter(
        "The PDFs to merge, in the order they should appear: the first item's pages come "
        "first. Each item is one PDF: its file_id ('f_...', from store_file, create_upload "
        "or an earlier Morphyard tool), the PDF itself as base64 in file_content plus "
        "its file_name, or a url to download it from. The file name (or the stored name) becomes that file's bookmark. "
        f"2 to {pdf_pages.MAX_MERGE_FILES} files, at most {pdf_pages.MAX_MERGE_TOTAL_MB} MB "
        "together.",
        min_length=2, max_length=pdf_pages.MAX_MERGE_FILES)],
    sources=_merge_sources,
)


def register_pdf_page_tools(mcp: FastMCP):
    """Register pdf_merge, pdf_delete_pages, pdf_reorder_pages, pdf_watermark and
    pdf_page_numbers"""

    add_operation_tool(mcp, "pdf_merge", """Merge several PDFs into one PDF, in the given order.

        Pass the PDFs in `files`, each as a file_id or as base64 in file_content.
        This tool has no single file_content upload slot: when the user's PDFs are
        upload references (e.g. [Uploaded Artifact: "a.pdf"]), call store_file once
        per upload with the reference in its file_content, collect the file_ids it
        returns, then call this tool with files=[{"file_id": ...}, ...] in the
        order wanted. Encrypted PDFs are refused: remove their password first.
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
