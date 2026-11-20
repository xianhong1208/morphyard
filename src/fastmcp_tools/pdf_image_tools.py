"""MCP tools: PDF pages to images, images to a PDF

Registered from register_morphyard_tools. The input triple and the
`output_schema=None` rule are the same as in morphyard_tools.py (read its module
docstring).

Why several pages come back as one ZIP rather than one File block per page: a File
whose MIME type is image/* is an image resource, and some MCP clients turn image
resources into image input for the model, so a 30-page render would put 30 full
page images into the model's context. A ZIP is opaque to the model and is one
download for the user, with the pages named in order. A single page comes back as
the image itself. The ZIP is also what images_to_pdf takes back.

Why images_to_pdf takes a ZIP for several images: the host injects one file per
call (see the note on pdf_merge in morphyard_tools.py). Several files in one call
come with 1.4.
"""

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.utilities.types import File
from pydantic import Field

from src.domain.convert import pdf_images
from src.domain.convert.limiter import conversion_slot
from src.domain.convert.service import (
    MAX_INPUT_MB,
    _decode_file_content,
    resolve_artifact_input,
)
from src.domain.convert.types import UnsupportedFormatError, mime_type_for, stem_of
from src.fastmcp_tools.morphyard_tools import _FILE_CONTENT, _FILE_NAME, _MIME_TYPE
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

_MIME = {"zip": "application/zip"}


class _TypedFile(File):
    """A File labelled with the real MIME type (fastmcp would say application/<format>)"""

    def __init__(self, data: bytes, fmt: str, name: str):
        super().__init__(data=data, format=fmt, name=name)
        self._output_mime_type = _MIME.get(fmt) or mime_type_for(fmt)

    def to_resource_content(self, mime_type=None, annotations=None):
        return super().to_resource_content(
            mime_type=mime_type or self._output_mime_type, annotations=annotations,
        )


def _read_upload(file_content: str | None, file_name: str | None) -> bytes:
    """Decode the upload without format detection (a ZIP of images is not a format
    resolve_artifact_input knows), with the same size limit"""
    if not file_content or not file_content.strip():
        raise UnsupportedFormatError(
            "No file provided. Upload a file first; the host injects it into "
            "file_content automatically."
        )
    raw, _ = _decode_file_content(file_content)
    if len(raw) > MAX_INPUT_MB * 1024 * 1024:
        raise UnsupportedFormatError(
            f"File too large: {len(raw) / 1024 / 1024:.1f} MB, limit is {MAX_INPUT_MB} MB. "
            "Split the images over several calls.",
            file_name=file_name,
        )
    return raw


def register_pdf_image_tools(mcp: FastMCP):

    @mcp.tool(output_schema=None)
    async def pdf_to_images(
        image_format: Annotated[str, Field(
            description="png (sharp text and lines, the default), jpg (smaller, for "
                        "scans and photos) or webp")] = "png",
        dpi: Annotated[int, Field(
            description=f"Resolution, {pdf_images.MIN_DPI}-{pdf_images.MAX_DPI}. 150 "
                        f"(default) is right for reading on screen, 300 for printing "
                        f"or OCR, 72-100 for thumbnails. Very large pages are rendered "
                        f"at a lower DPI so one page stays under "
                        f"{pdf_images.MAX_PAGE_PIXELS // 1_000_000} megapixels")]
            = pdf_images.DEFAULT_DPI,
        pages: Annotated[str, Field(
            description="Pages to render, 1-based and inclusive, e.g. '1', '1-3,7' or "
                        "'10-'. Empty means every page. At most "
                        f"{pdf_images.MAX_RENDER_PAGES} pages per call")] = "",
        quality: Annotated[int, Field(
            description="JPEG / WebP quality, 1-95 (ignored for png)")]
            = pdf_images.DEFAULT_QUALITY,
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Render PDF pages as images, one image per page.

        One page comes back as a single image file; several pages come back as
        one ZIP containing one image per page, named <name>_p01.png,
        <name>_p02.png ... in the order requested. Use this when the user wants
        a page as a picture (to share, embed in a slide, or post), a thumbnail,
        or a page for another image tool. To read a PDF's text, use
        extract_text instead.
        """
        source = resolve_artifact_input(file_content, file_name, mime_type)
        if source.fmt != "pdf":
            return [
                f"This is not a PDF (detected {source.fmt}). Convert it to PDF with "
                "convert_document first."
            ]

        pdf_images.validate_render_args(image_format, dpi, quality)
        async with conversion_slot():
            rendered = await pdf_images.render_pages(
                source.data, image_format, dpi, pages, quality,
            )

        fmt = rendered.fmt
        count = len(rendered.images)
        notes = ""
        if rendered.lowered:
            listed = ", ".join(f"page {p} at {d} DPI" for p, d in rendered.lowered.items())
            notes = (f"\nLarge pages were rendered at a lower resolution to stay under "
                     f"{pdf_images.MAX_PAGE_PIXELS // 1_000_000} megapixels: {listed}.")
        if count == 1:
            number, blob = rendered.images[0]
            name = f"{source.name}_p{number}"
            logger.info(f"[DELIVER] {name}.{fmt} ({len(blob) / 1024:.1f} KB)")
            return [
                f"Rendered page {number} of {rendered.total_pages} as {fmt.upper()} at "
                f"{rendered.dpi} DPI, {len(blob) / 1024:.1f} KB.{notes}",
                _TypedFile(blob, fmt, name),
            ]

        width = max(2, len(str(rendered.total_pages)))
        entries = [(f"{source.name}_p{number:0{width}d}.{fmt}", blob)
                   for number, blob in rendered.images]
        archive = pdf_images.zip_images(entries)
        first, last = entries[0][0], entries[-1][0]
        logger.info(f"[DELIVER] {source.name}_pages.zip ({len(archive) / 1024:.1f} KB)")
        return [
            f"Rendered {count} of {rendered.total_pages} pages as {fmt.upper()} at "
            f"{rendered.dpi} DPI into one ZIP ({len(archive) / 1024:.1f} KB): {first} "
            f"... {last}, one image per page in the order requested.{notes}",
            _TypedFile(archive, "zip", f"{source.name}_pages"),
        ]

    @mcp.tool(output_schema=None)
    async def images_to_pdf(
        page_size: Annotated[str, Field(
            description="'fit' (default): each page is the size of its image. 'a4' or "
                        "'letter': every page that size, the image scaled to fit inside "
                        "the margins and centred")] = "fit",
        orientation: Annotated[str, Field(
            description="For a4 / letter: 'auto' (default) turns each page to match its "
                        "image (landscape for wide images), or force 'portrait' / "
                        "'landscape'. Ignored for 'fit'")] = "auto",
        margin_mm: Annotated[float | None, Field(
            description="White margin around each image in millimetres, 0-50. Default: "
                        "0 for 'fit', 10 for a4 / letter")] = None,
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Combine images into one PDF, one image per page.

        The uploaded file is either one image (PNG, JPEG, WebP, TIFF, GIF, BMP;
        every page of a multi-page TIFF becomes a page, GIF / WebP animations
        give their first frame) or a ZIP of images. Only one file can be passed
        per call, so for several images ask the user to put them in a ZIP: the
        pages follow the file names in natural order (page2 before page10), and
        files that are not images are skipped and listed. Photos are turned
        upright from their EXIF orientation, transparency becomes white, and
        JPEGs are embedded unchanged (no quality loss).
        """
        raw = _read_upload(file_content, file_name)
        if raw.lstrip()[:5] == b"%PDF-":
            return ["This is already a PDF; nothing to combine."]

        src = "zip" if raw.startswith(b"PK\x03\x04") else "image"
        pdf_images.validate_layout(page_size, orientation, margin_mm, src)
        async with conversion_slot():
            built = await pdf_images.build_pdf(raw, page_size, orientation, margin_mm, src)

        name = stem_of(file_name)
        summary: list[str] = [
            f"Created a {built.pages}-page PDF ({len(built.data) / 1024:.1f} KB)"
            + (f" from {', '.join(built.labels)}" if src == "zip" and built.pages <= 20
               else "") + "."
        ]
        if built.passed_through:
            summary.append(f"{built.passed_through} JPEG image(s) were embedded unchanged.")
        if built.skipped:
            summary.append(
                "Skipped (not images this tool reads): " + ", ".join(built.skipped[:20])
                + (" ..." if len(built.skipped) > 20 else "") + ". Tell the user."
            )
        logger.info(f"[DELIVER] {name}.pdf ({len(built.data) / 1024:.1f} KB)")
        result: list[Any] = [" ".join(summary), _TypedFile(built.data, "pdf", name)]
        return result
