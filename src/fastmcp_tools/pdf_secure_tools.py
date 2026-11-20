"""MCP tools for PDF compression, protection, unlocking and metadata (pikepdf).

Same contracts as morphyard_tools.py: the file_content / file_name / mime_type
triple, and `@mcp.tool(output_schema=None)` on every tool that returns a list
of content blocks. Operations that rewrite the file take a conversion slot;
pdf_metadata only reads, like inspect_document, and takes none.
"""

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.utilities.types import File
from pydantic import Field

from src.domain.convert import pdf_secure
from src.domain.convert.limiter import conversion_slot
from src.domain.convert.service import resolve_artifact_input
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

_FILE_CONTENT = Annotated[str | None, Field(
    description="The PDF uploaded by the user. If you see a reference such as "
                "[Uploaded Artifact: \"xxx\"], put it in this parameter; the host "
                "replaces it with the actual file content automatically.")]
_FILE_NAME = Annotated[str | None, Field(
    description="Original file name (with extension). Filled in by the host "
                "automatically; you do not need to set it. Never put an upload "
                "reference here.")]
_MIME_TYPE = Annotated[str | None, Field(description="MIME type (optional, usually empty)")]

_PASSWORD_NOTE = (
    "Passwords given to this tool appear in the conversation history; suggest the user "
    "does not reuse a password from another system."
)


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def _pdf_source(file_content, file_name, mime_type):
    """The decoded upload, or a refusal text when it is not a PDF.

    Text-only answers are returned as a plain string: a list holding only strings
    would reach the client as one JSON-encoded text block.
    """
    source = resolve_artifact_input(file_content, file_name, mime_type)
    if source.fmt != "pdf":
        return source, (f"This is not a PDF (detected {source.fmt}). "
                        "Convert it to PDF with convert_document first.")
    return source, None


def register_pdf_secure_tools(mcp: FastMCP):
    """Register pdf_compress, pdf_protect, pdf_unlock, pdf_metadata, pdf_strip_metadata."""

    @mcp.tool(output_schema=None)
    async def pdf_compress(
        level: Annotated[str, Field(
            description="'lossless': restructure only (object streams, recompressed "
                        "streams, unused objects removed); pages look identical. "
                        "'balanced' (default): also downsample images above 150 DPI and "
                        "re-encode them as JPEG quality 75; good for sharing and email. "
                        "'small': images to 96 DPI, JPEG quality 50; smallest, visibly "
                        "softer images.")] = "balanced",
        max_dpi: Annotated[int, Field(
            description="Optional override of the level's image resolution (36-600). "
                        "0 = the level's default. Not allowed with 'lossless'.")] = 0,
        jpeg_quality: Annotated[int, Field(
            description="Optional override of the level's JPEG quality (10-95). "
                        "0 = the level's default. Not allowed with 'lossless'.")] = 0,
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list | str:
        """Make a PDF file smaller and return it, with the before/after sizes.

        Text, vector graphics and fonts are never degraded; only images are
        downsampled/re-encoded, and only at 'balanced' or 'small'. Scanned or
        photo-heavy PDFs shrink the most; text-only PDFs shrink a little or not at
        all. If the result is not smaller, the original file is returned unchanged
        and the response says so; tell the user rather than offering it as a
        smaller file. Password-protected PDFs must be unlocked first (pdf_unlock).
        """
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return refusal
        async with conversion_slot():
            r = await pdf_secure.compress_pdf(source.data, level, max_dpi or None,
                                              jpeg_quality or None)
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
        return [summary, File(data=r.data, format="pdf", name=name)]

    @mcp.tool(output_schema=None)
    async def pdf_protect(
        password: Annotated[str, Field(
            description="Open password: needed to open the PDF at all. Leave empty for "
                        "a PDF anyone can open but whose permissions are restricted.")] = "",
        owner_password: Annotated[str, Field(
            description="Owner password: lifts the restrictions later. Required when "
                        "any allow_* is false, and must differ from the open password. "
                        "Leave empty when only setting an open password.")] = "",
        allow_print: Annotated[bool, Field(description="Readers may print")] = True,
        allow_copy: Annotated[bool, Field(
            description="Readers may copy text and images (screen readers keep "
                        "access either way)")] = True,
        allow_modify: Annotated[bool, Field(
            description="Readers may edit the document and insert/delete/rotate "
                        "pages")] = True,
        allow_annotate: Annotated[bool, Field(
            description="Readers may add comments and fill forms")] = True,
        allow_fill_forms: Annotated[bool, Field(
            description="Readers may fill in form fields")] = True,
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list | str:
        """Encrypt a PDF with AES-256: an open password, permission restrictions, or both.

        - "Put a password on this PDF" -> password only.
        - "Anyone may read it but not print or copy" -> owner_password plus
          allow_print=false, allow_copy=false, no password.
        - Both -> password and owner_password (different) plus the allow_* flags.

        Permission restrictions are honoured by standard PDF readers; they are not
        DRM and a determined user with other tools can ignore them, so say so if
        the user relies on them. An already encrypted PDF must be unlocked first.
        """
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return refusal
        allow = {"print": allow_print, "copy": allow_copy, "modify": allow_modify,
                 "annotate": allow_annotate, "fill_forms": allow_fill_forms}
        async with conversion_slot():
            data, info = await pdf_secure.protect_pdf(source.data, password,
                                                       owner_password, allow)
        parts = ["Encrypted with AES-256."]
        parts.append("An open password is required to open it." if info["open_password"]
                     else "Anyone can open it.")
        if info["restricted"]:
            parts.append("Not allowed without the owner password: "
                         + ", ".join(k.replace("_", " ") for k in info["restricted"]) + ".")
        parts.append(_PASSWORD_NOTE)
        return [" ".join(parts), File(data=data, format="pdf", name=f"{source.name}_protected")]

    @mcp.tool(output_schema=None)
    async def pdf_unlock(
        password: Annotated[str, Field(
            description="The PDF's password, as given by the user. The owner password "
                        "always works; the open password works when the owner set no "
                        "permission restrictions.")],
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list | str:
        """Remove the password and permission restrictions from a PDF, given its password.

        Returns an unencrypted copy. Only works with the correct password; there is
        no password recovery or guessing. A wrong password gives a plain error; do
        not retry with guessed passwords, ask the user. If the file is not
        encrypted, nothing is returned but a note saying so.
        """
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return refusal
        async with conversion_slot():
            data = await pdf_secure.unlock_pdf(source.data, password)
        if data is None:
            return "This PDF is not encrypted; there is nothing to unlock."
        return [
            "Unlocked: the returned copy has no password and no permission restrictions.",
            File(data=data, format="pdf", name=f"{source.name}_unlocked"),
        ]

    @mcp.tool()
    async def pdf_metadata(
        password: Annotated[str, Field(
            description="Only for a PDF that needs a password to open; otherwise "
                        "leave empty")] = "",
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> dict[str, Any]:
        """Report a PDF's metadata without changing it.

        Returns: pdf_version, page_count, encrypted (+ method, permissions),
        info (the document information dictionary: Title, Author, Creator,
        Producer, CreationDate, ModDate...), xmp (XMP properties such as
        dc:creator, xmp:CreatorTool, pdf:Producer), annotations (count and author
        names), javascript (document scripts, actions), embedded_files, has_form.
        Use it to answer "who made this / what is hidden in this PDF", and before
        pdf_strip_metadata to show the user what would be removed.
        """
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return {"error": refusal}
        return await pdf_secure.read_metadata(source.data, password)

    @mcp.tool(output_schema=None)
    async def pdf_strip_metadata(
        remove_annotation_authors: Annotated[bool, Field(
            description="Remove author names and dates from comments/annotations "
                        "(the comments themselves stay)")] = True,
        remove_javascript: Annotated[bool, Field(
            description="Also remove document JavaScript and actions (open actions, "
                        "page/field actions). May break interactive forms that "
                        "calculate")] = False,
        remove_attachments: Annotated[bool, Field(
            description="Also remove embedded files and file-attachment "
                        "annotations")] = False,
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list | str:
        """Return a copy of the PDF with its metadata removed.

        Always removed: the document information dictionary (Title, Author,
        Subject, Keywords, Creator, Producer, dates), every XMP metadata stream
        (document, pages, images), application private data (PieceInfo), and the
        file identifier is regenerated. Optional: annotation authors (on by
        default), JavaScript, attachments. Page content (text, images, comments'
        text, form fields) is not changed, and author names written in the page
        text itself are not detected. Use pdf_metadata first to see what is there.
        """
        source, refusal = _pdf_source(file_content, file_name, mime_type)
        if refusal:
            return refusal
        async with conversion_slot():
            r = await pdf_secure.strip_metadata(source.data, remove_annotation_authors,
                                                remove_javascript, remove_attachments)
        done = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in r.removed.items() if v)
        summary = ("Metadata removed (" + (done or "there was nothing to remove") + "). "
                   f"Size {_kb(len(source.data))} -> {_kb(len(r.data))}.")
        return [summary, File(data=r.data, format="pdf", name=f"{source.name}_clean")]

    logger.info("PDF compress / protect / unlock / metadata tools registered")
