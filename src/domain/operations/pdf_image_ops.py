"""Operations: PDF pages to images, images to a PDF (pdf_images.py).

Why several pages come back as one ZIP rather than one file per page: a file
whose MIME type is image/* is an image resource, and some MCP clients turn image
resources into image input for the model, so a 30-page render would put 30 full
page images into the model's context. A ZIP is opaque to the model and is one
download for the user, with the pages named in order. A single page comes back as
the image itself. The ZIP is also what images_to_pdf takes back.

images_to_pdf takes several images as several inputs (the tool's `files`), or one image,
or a ZIP of images (for hosts that pass one file per call).
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from src.domain.convert import pdf_images
from src.domain.convert.limiter import conversion_slot
from src.domain.convert.types import IMAGE_FORMATS
from src.domain.operations.base import (
    Arity,
    OpCall,
    Operation,
    OperationRefused,
    OpResult,
    OutputFile,
    register,
)
from src.domain.operations.pdf_basic_ops import PDF, not_a_pdf
from src.log import get_mcptools_logger

logger = get_mcptools_logger()


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------
# pdf_to_images
# ----------------------------------------------------------------------------

class PdfToImagesArgs(_Args):
    image_format: Annotated[str, Field(
        description="png (sharp text and lines, the default), jpg (smaller, for "
                    "scans and photos) or webp")] = "png"
    dpi: Annotated[int, Field(
        description=f"Resolution, {pdf_images.MIN_DPI}-{pdf_images.MAX_DPI}. 150 "
                    f"(default) is right for reading on screen, 300 for printing "
                    f"or OCR, 72-100 for thumbnails. Very large pages are rendered "
                    f"at a lower DPI so one page stays under "
                    f"{pdf_images.MAX_PAGE_PIXELS // 1_000_000} megapixels")] \
        = pdf_images.DEFAULT_DPI
    pages: Annotated[str, Field(
        description="Pages to render, 1-based and inclusive, e.g. '1', '1-3,7' or "
                    "'10-'. Empty means every page. At most "
                    f"{pdf_images.MAX_RENDER_PAGES} pages per call")] = ""
    quality: Annotated[int, Field(
        description="JPEG / WebP quality, 1-95 (ignored for png)")] \
        = pdf_images.DEFAULT_QUALITY


async def _pdf_to_images(call: OpCall) -> OpResult:
    source, a = call.inputs[0], call.args
    # Checked after the input, as the tool always did: a non-PDF is refused first.
    pdf_images.validate_render_args(a.image_format, a.dpi, a.quality)
    async with conversion_slot():
        rendered = await pdf_images.render_pages(
            source.data, a.image_format, a.dpi, a.pages, a.quality,
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
        return OpResult(
            summary=f"Rendered page {number} of {rendered.total_pages} as {fmt.upper()} at "
                    f"{rendered.dpi} DPI, {len(blob) / 1024:.1f} KB.{notes}",
            files=[OutputFile(blob, fmt, name)],
        )

    width = max(2, len(str(rendered.total_pages)))
    entries = [(f"{source.name}_p{number:0{width}d}.{fmt}", blob)
               for number, blob in rendered.images]
    archive = pdf_images.zip_images(entries)
    first, last = entries[0][0], entries[-1][0]
    logger.info(f"[DELIVER] {source.name}_pages.zip ({len(archive) / 1024:.1f} KB)")
    return OpResult(
        summary=f"Rendered {count} of {rendered.total_pages} pages as {fmt.upper()} at "
                f"{rendered.dpi} DPI into one ZIP ({len(archive) / 1024:.1f} KB): {first} "
                f"... {last}, one image per page in the order requested.{notes}",
        files=[OutputFile(archive, "zip", f"{source.name}_pages")],
    )


register(Operation(
    name="pdf_to_images",
    title="Render PDF pages as images (one image, or a ZIP of several)",
    args_model=PdfToImagesArgs,
    arity=Arity.ONE_TO_ONE,
    run=_pdf_to_images,
    input_formats=PDF,
    output_formats=frozenset(pdf_images.RENDER_FORMATS) | {"zip"},
    wrong_input=not_a_pdf,
))


# ----------------------------------------------------------------------------
# images_to_pdf
# ----------------------------------------------------------------------------

class ImagesToPdfArgs(_Args):
    page_size: Annotated[str, Field(
        description="'fit' (default): each page is the size of its image. 'a4' or "
                    "'letter': every page that size, the image scaled to fit inside "
                    "the margins and centred")] = "fit"
    orientation: Annotated[str, Field(
        description="For a4 / letter: 'auto' (default) turns each page to match its "
                    "image (landscape for wide images), or force 'portrait' / "
                    "'landscape'. Ignored for 'fit'")] = "auto"
    margin_mm: Annotated[float | None, Field(
        description="White margin around each image in millimetres, 0-50. Default: "
                    "0 for 'fit', 10 for a4 / letter")] = None


def _is_pdf(raw: bytes) -> bool:
    return raw.lstrip()[:5] == b"%PDF-"


def _image_label(position: int, source) -> str:
    return (source.file_name or "").strip() or f"image {position}"


async def _images_to_pdf(call: OpCall) -> OpResult:
    source, a = call.inputs[0], call.args
    if len(call.inputs) == 1:
        raw = source.data
        if _is_pdf(raw):
            raise OperationRefused("This is already a PDF; nothing to combine.")
        src = "zip" if raw.startswith(b"PK\x03\x04") else "image"
        data: bytes | list[tuple[str, bytes]] = raw
    else:
        # Several files: one page per image, in the order given.
        for position, item in enumerate(call.inputs, start=1):
            if _is_pdf(item.data):
                raise OperationRefused(
                    f"File #{position} in files ({_image_label(position, item)}) is a PDF, "
                    "not an image. To combine PDFs use pdf_merge.")
        src = "image"
        data = [(_image_label(position, item), item.data)
                for position, item in enumerate(call.inputs, start=1)]
    pdf_images.validate_layout(a.page_size, a.orientation, a.margin_mm, src)
    async with conversion_slot():
        built = await pdf_images.build_pdf(data, a.page_size, a.orientation, a.margin_mm, src)

    name = source.name
    summary: list[str] = [
        f"Created a {built.pages}-page PDF ({len(built.data) / 1024:.1f} KB)"
        + (f" from {', '.join(built.labels)}"
           if (src == "zip" or len(call.inputs) > 1) and built.pages <= 20 else "") + "."
    ]
    if built.passed_through:
        summary.append(f"{built.passed_through} JPEG image(s) were embedded unchanged.")
    if built.skipped:
        summary.append(
            "Skipped (not images this tool reads): " + ", ".join(built.skipped[:20])
            + (" ..." if len(built.skipped) > 20 else "") + ". Tell the user."
        )
    logger.info(f"[DELIVER] {name}.pdf ({len(built.data) / 1024:.1f} KB)")
    return OpResult(summary=" ".join(summary), files=[OutputFile(built.data, "pdf", name)])


register(Operation(
    name="images_to_pdf",
    title="Combine images (several images, one image, or a ZIP of images) into one PDF",
    args_model=ImagesToPdfArgs,
    arity=Arity.MANY_TO_ONE,
    run=_images_to_pdf,
    # Read as raw bytes: a ZIP of images is not a format the detector knows, so
    # the format is checked by the operation itself (a PDF is refused).
    detect_input_format=False,
    input_formats=frozenset(IMAGE_FORMATS) | {"zip"},
    output_formats=PDF,
    wrong_input=lambda artifact, _position: "This is already a PDF; nothing to combine.",
))
