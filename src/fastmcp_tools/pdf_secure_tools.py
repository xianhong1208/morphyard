"""MCP tools for PDF compression, protection, unlocking and metadata (pikepdf).

Thin wrappers over the operations in src/domain/operations/pdf_secure_ops.py,
built by operation_tool.add_operation_tool(). Same contracts as
morphyard_tools.py: the file_content / file_name / mime_type triple. A refusal,
or an answer without a file, is returned as plain text: a list holding only
strings would reach the client as one JSON-encoded text block.
"""

from typing import Any

from fastmcp import FastMCP

from src.fastmcp_tools.operation_tool import PDF_FILE_CONTENT, Reply, add_operation_tool, upload
from src.log import get_mcptools_logger

logger = get_mcptools_logger()


def register_pdf_secure_tools(mcp: FastMCP):
    """Register pdf_compress, pdf_protect, pdf_unlock, pdf_metadata, pdf_strip_metadata."""
    pdf = upload(PDF_FILE_CONTENT)

    add_operation_tool(mcp, "pdf_compress", """Make a PDF file smaller and return it, with the before/after sizes.

        Text, vector graphics and fonts are never degraded; only images are
        downsampled/re-encoded, and only at 'balanced' or 'small'. Scanned or
        photo-heavy PDFs shrink the most; text-only PDFs shrink a little or not at
        all. If the result is not smaller, the original file is returned unchanged
        and the response says so; tell the user rather than offering it as a
        smaller file. Password-protected PDFs must be unlocked first (pdf_unlock).
        """, reply=Reply.FILES_OR_TEXT, returns=list | str, inputs=pdf)

    add_operation_tool(mcp, "pdf_protect", """Encrypt a PDF with AES-256: an open password, permission restrictions, or both.

        - "Put a password on this PDF" -> password only.
        - "Anyone may read it but not print or copy" -> owner_password plus
          allow_print=false, allow_copy=false, no password.
        - Both -> password and owner_password (different) plus the allow_* flags.

        Permission restrictions are honoured by standard PDF readers; they are not
        DRM and a determined user with other tools can ignore them, so say so if
        the user relies on them. An already encrypted PDF must be unlocked first.
        """, reply=Reply.FILES_OR_TEXT, returns=list | str, inputs=pdf)

    add_operation_tool(mcp, "pdf_unlock", """Remove the password and permission restrictions from a PDF, given its password.

        Returns an unencrypted copy. Only works with the correct password; there is
        no password recovery or guessing. A wrong password gives a plain error; do
        not retry with guessed passwords, ask the user. If the file is not
        encrypted, nothing is returned but a note saying so.
        """, reply=Reply.FILES_OR_TEXT, returns=list | str, inputs=pdf)

    add_operation_tool(mcp, "pdf_metadata", """Report a PDF's metadata without changing it.

        Returns: pdf_version, page_count, encrypted (+ method, permissions),
        info (the document information dictionary: Title, Author, Creator,
        Producer, CreationDate, ModDate...), xmp (XMP properties such as
        dc:creator, xmp:CreatorTool, pdf:Producer), annotations (count and author
        names), javascript (document scripts, actions), embedded_files, has_form.
        Use it to answer "who made this / what is hidden in this PDF", and before
        pdf_strip_metadata to show the user what would be removed.
        """, reply=Reply.REPORT, returns=dict[str, Any], inputs=pdf)

    add_operation_tool(mcp, "pdf_strip_metadata", """Return a copy of the PDF with its metadata removed.

        Always removed: the document information dictionary (Title, Author,
        Subject, Keywords, Creator, Producer, dates), every XMP metadata stream
        (document, pages, images), application private data (PieceInfo), and the
        file identifier is regenerated. Optional: annotation authors (on by
        default), JavaScript, attachments. Page content (text, images, comments'
        text, form fields) is not changed, and author names written in the page
        text itself are not detected. Use pdf_metadata first to see what is there.
        """, reply=Reply.FILES_OR_TEXT, returns=list | str, inputs=pdf)

    logger.info("PDF compress / protect / unlock / metadata tools registered")
