"""Morphyard MCP tools

Input contract: file_content (base64) / file_name / mime_type, injected
automatically by the host application's file-injection (artifact) mechanism.
The parameter names must not change; the host matches on them (the reasons for
their wording are in operation_tool.py).

Output contract: a list of [text summary, File(...)], where File comes from
fastmcp.utilities.types; the client receives a downloadable file directly
(the same pattern other MCP servers use).

What each tool does lives in the operations registry (src/domain/operations/):
its arguments, accepted formats and the work itself. The tools here are thin
wrappers built by operation_tool.add_operation_tool(): this module holds what is
MCP-specific, i.e. each tool's description (what the model reads), where the
upload parameters go, and the shape of its reply.
"""

from typing import Any

from fastmcp import FastMCP

from src.domain.operations.convert_ops import fidelity_warning as _fidelity_warning
from src.fastmcp_tools.job_tools import register_job_tools
from src.fastmcp_tools.operation_tool import Reply, add_operation_tool, upload
from src.fastmcp_tools.pdf_image_tools import register_pdf_image_tools
from src.fastmcp_tools.pdf_page_tools import register_pdf_page_tools
from src.fastmcp_tools.pdf_secure_tools import register_pdf_secure_tools
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

__all__ = ["register_morphyard_tools", "_fidelity_warning"]


def register_morphyard_tools(mcp: FastMCP):
    """Register every Morphyard tool (config.yaml's function_name points here)"""

    # ------------------------------------------------------------------
    # Capability queries
    # ------------------------------------------------------------------

    add_operation_tool(mcp, "list_supported_conversions", """List the conversion pairs this service actually supports.

        Before calling convert_document, use this tool if you are unsure whether a
        conversion is possible. It returns the paths available given the
        **engines actually installed** on this server, not a theoretical list.

        Args:
            source_format: Optional. When set, only that format's targets are returned.

        Returns:
            supported: {source format: [reachable target formats]}
            engines: availability of each engine
            note: unlisted pairs may still work via an intermediate format; just try
                  convert_document
        """, reply=Reply.REPORT, returns=dict[str, Any])

    add_operation_tool(mcp, "inspect_document", """Show basic information about an uploaded file: detected format, size,
        and the target formats it can be converted to.

        When the user uploads a file without saying what to do with it, use this
        tool first to see what you have, then decide which conversion to
        suggest. For PDFs the page count is also returned.
        """, reply=Reply.REPORT, returns=dict[str, Any], inputs=upload())

    # ------------------------------------------------------------------
    # Main conversion
    # ------------------------------------------------------------------

    add_operation_tool(mcp, "convert_document", """Convert the uploaded file to the given format and return the converted file.

        Supports conversions between Office documents, PDF, Markdown, HTML,
        spreadsheets, presentations and images. The source format is detected
        automatically from the file; no need to specify it.

        Typical usage:
          - "Turn this Word file into a PDF"     -> target_format="pdf"
          - "Show me what is in this PDF"        -> target_format="md"
          - "Make a Word document from this Markdown" -> target_format="docx"
          - "Convert this image to JPG"          -> target_format="jpg"

        If the response contains a quality warning, relay it to the user proactively.
        """, reply=Reply.FILES, returns=list, inputs=upload(), inputs_at=2, progress=True)

    add_operation_tool(mcp, "extract_text", """Extract the document's text as Markdown and return it directly (no file).

        Use this when you need to *read* the user's uploaded document to answer
        questions, summarize or analyze it, rather than convert_document: this
        tool returns text you can read directly.

        Supports PDF, Word, Excel, PowerPoint, HTML, CSV and images (OCR).
        """, reply=Reply.REPORT, returns=str, inputs=upload(), inputs_at=0)

    # ------------------------------------------------------------------
    # PDF operations
    # ------------------------------------------------------------------

    add_operation_tool(mcp, "pdf_extract_pages", """Extract the given pages from a PDF into a new PDF.

        Page order matters: pages='3,1' produces a PDF with page 3 first and
        page 1 second, which can be used to reorder pages.
        """, reply=Reply.FILES, returns=list, inputs=upload())

    add_operation_tool(mcp, "pdf_split", """Split a PDF into several files and return all of them.

        Note: a PDF with many pages produces many files. Check the page count
        with inspect_document first and confirm the split with the user.
        """, reply=Reply.FILES, returns=list, inputs=upload())

    add_operation_tool(mcp, "pdf_rotate", """Rotate the given pages of a PDF (commonly used to fix wrongly oriented scans).""",
                       reply=Reply.FILES, returns=list, inputs=upload())

    # pdf_compress, pdf_protect, pdf_unlock, pdf_metadata, pdf_strip_metadata
    register_pdf_secure_tools(mcp)

    # pdf_merge, pdf_delete_pages, pdf_reorder_pages, pdf_watermark, pdf_page_numbers
    register_pdf_page_tools(mcp)

    # PDF pages <-> images (1.3)
    register_pdf_image_tools(mcp)

    # job_status, job_cancel, job_list (1.4): conversions that outlive their call
    register_job_tools(mcp)

    logger.info("Morphyard MCP tools registered")
