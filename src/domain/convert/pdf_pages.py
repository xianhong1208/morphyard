"""PDF page tools: merge, delete, reorder, watermark, page numbers and header/footer

pdf -> pdf operations like pdfops.py, kept in a module of their own. Pages are
handled with pypdf; text and image overlays are drawn with reportlab and laid
over each page with pypdf.

Every overlay is drawn in the page's *displayed* coordinate space (the visible
crop box, turned the way the page's /Rotate turns it) and then mapped back into
the page's own user space. So a watermark or a page number sits upright where a
reader sees it, on portrait, landscape and rotated pages alike, and on documents
that mix page sizes.

Fonts: reportlab can embed (and subset) TrueType fonts only. The Noto CJK fonts
installed for LibreOffice are OpenType with CFF outlines, which reportlab cannot
embed, so text is drawn with DejaVu Sans for Latin script and Droid Sans
Fallback (Apache-2.0, TrueType) for Chinese, Japanese and Korean, switching font
per run of characters. When neither is installed the text falls back to
reportlab's built-in CID font for Traditional Chinese, which is not embedded:
viewers then substitute a CJK font of their own.
"""

import asyncio
import io
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache

from src.domain.convert.pdfops import parse_page_range
from src.domain.convert.types import ConversionFailedError
from src.domain.exceptions import DomainException
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# Limits. Inputs are already capped at 50 MB each by service.MAX_INPUT_MB.
MAX_MERGE_FILES = 20
MAX_MERGE_TOTAL_MB = 50
MAX_PAGES = 2000                      # per input document, and for a merged result
MAX_OUTPUT_MB = 100
MAX_TEXT_CHARS = 200                  # watermark text, header, footer, number format
MAX_WATERMARK_IMAGE_MB = 10
MAX_WATERMARK_IMAGE_PIXELS = 40_000_000
MAX_TILES_PER_PAGE = 300
MIN_FIT_FONT_SIZE = 1.0               # text shrunk to fit the page never goes below this

WATERMARK_POSITIONS = ("center", "diagonal", "tiled")
NUMBER_POSITIONS = (
    "bottom-center", "bottom-left", "bottom-right", "top-center", "top-left", "top-right",
)
ALIGNMENTS = ("left", "center", "right")

_LATIN_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
_CJK_FONTS = (
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts-droid-fallback/truetype/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/google-droid-sans-fonts/DroidSansFallbackFull.ttf",
)
_CID_FALLBACK = "MSung-Light"         # Adobe-CNS1, Traditional Chinese, never embedded


class InvalidPdfOptionError(DomainException):
    """A tool option is out of range or malformed; the message says which and why"""

    def __init__(self, message: str):
        super().__init__(message=message, error_code="INVALID_OPTION")


def _failed(reason: str) -> ConversionFailedError:
    return ConversionFailedError("pypdf", "pdf", "pdf", reason)


# ----------------------------------------------------------------------------
# Reading and writing
# ----------------------------------------------------------------------------

def _open(data: bytes, label: str = "the PDF"):
    """Read a PDF, refusing encrypted ones and ones over the page limit"""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted
    except Exception as e:
        raise _failed(f"{label} is not a readable PDF: {str(e)[:300]}")
    if encrypted:
        raise _failed(
            f"{label} is encrypted (it has an open password or permission restrictions). "
            "Remove the password first, then call this tool again with the unlocked PDF."
        )
    try:
        count = len(reader.pages)
    except Exception as e:
        raise _failed(f"{label} is damaged; its pages cannot be read: {str(e)[:300]}")
    if count > MAX_PAGES:
        raise _failed(f"{label} has {count} pages; the limit is {MAX_PAGES}.")
    if count == 0:
        raise _failed(f"{label} has no pages.")
    return reader


def _write(writer) -> bytes:
    buffer = io.BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()
    if len(data) > MAX_OUTPUT_MB * 1024 * 1024:
        raise _failed(
            f"the result would be {len(data) / 1024 / 1024:.0f} MB; the limit is "
            f"{MAX_OUTPUT_MB} MB."
        )
    return data


def _clone(reader):
    """A writer holding the whole document: outline, links, forms and metadata kept"""
    from pypdf import PdfWriter

    return PdfWriter(clone_from=reader)


# ----------------------------------------------------------------------------
# Merge
# ----------------------------------------------------------------------------

def _merge(inputs: list[tuple[str, bytes]], bookmarks: bool) -> tuple[bytes, int]:
    from pypdf import PdfWriter

    readers = [_open(blob, f"file #{i} ({name!r})")
               for i, (name, blob) in enumerate(inputs, start=1)]
    total_pages = sum(len(r.pages) for r in readers)
    if total_pages > MAX_PAGES:
        raise _failed(
            f"the merged PDF would have {total_pages} pages; the limit is {MAX_PAGES}."
        )
    writer = PdfWriter()
    for (name, _), reader in zip(inputs, readers):
        # One top-level bookmark per input file, with that file's own outline
        # nested under it, so the merged document can still be navigated.
        writer.append(reader, outline_item=name if bookmarks else None,
                      import_outline=bookmarks)
    return _write(writer), total_pages


async def merge_pdfs(inputs: list[tuple[str, bytes]], bookmarks: bool = True,
                     ) -> tuple[bytes, int]:
    """Merge PDFs in the given order; returns (pdf, page count)

    inputs: [(display name used for the bookmark, pdf bytes), ...]
    """
    validate_merge_inputs(inputs)
    data, pages = await asyncio.to_thread(_merge, inputs, bookmarks)
    logger.info(f"[pdf_pages] merged {len(inputs)} files, {pages} pages → {len(data)} bytes")
    return data, pages


def validate_merge_inputs(inputs: list[tuple[str, bytes]]) -> None:
    """Count and size checks that need no PDF parsing (run before taking a slot)"""
    if len(inputs) < 2:
        raise InvalidPdfOptionError("pdf_merge needs at least 2 PDFs.")
    if len(inputs) > MAX_MERGE_FILES:
        raise InvalidPdfOptionError(
            f"pdf_merge takes at most {MAX_MERGE_FILES} files; got {len(inputs)}. "
            "Merge them in batches, then merge the results."
        )
    total = sum(len(blob) for _, blob in inputs)
    if total > MAX_MERGE_TOTAL_MB * 1024 * 1024:
        raise InvalidPdfOptionError(
            f"the files add up to {total / 1024 / 1024:.1f} MB; pdf_merge takes at most "
            f"{MAX_MERGE_TOTAL_MB} MB in one call."
        )


# ----------------------------------------------------------------------------
# Delete and reorder
# ----------------------------------------------------------------------------

def parse_page_order(spec: str, total: int) -> list[int]:
    """Parse a complete new page order into 0-based indices

    Accepts numbers and ranges, 1-based: "3,1,2,4-10". A descending range runs
    backwards ("10-1"), "5-" means page 5 to the last page, and "reverse" turns
    the whole document around. Every page must appear exactly once, so a typo
    can never drop or duplicate a page silently.
    """
    if not spec or not spec.strip():
        raise InvalidPdfOptionError(
            "order is empty. Give every page once in the new order, e.g. '3,1,2,4-10', "
            "or 'reverse'."
        )
    if spec.strip().lower() == "reverse":
        return list(range(total - 1, -1, -1))

    order: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.fullmatch(r"(\d+)?\s*-\s*(\d+)?|(\d+)", chunk)
        if not m or chunk == "-":
            raise InvalidPdfOptionError(
                f"invalid entry '{chunk}' in order (expected numbers and ranges such "
                "as '3,1,2,4-10')."
            )
        if m.group(3):
            start = end = int(m.group(3))
        else:
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else total
        for n in (start, end):
            if n < 1 or n > total:
                raise InvalidPdfOptionError(
                    f"page {n} in '{chunk}' does not exist (the document has {total} pages)."
                )
        step = 1 if end >= start else -1
        order.extend(range(start - 1, end - 1 + step, step))

    seen: set[int] = set()
    repeated = sorted({p + 1 for p in order if p in seen or seen.add(p)})
    if repeated:
        raise InvalidPdfOptionError(
            f"order lists page(s) {_compact(repeated)} more than once; every page must "
            "appear exactly once. To leave pages out, use pdf_delete_pages."
        )
    missing = [p + 1 for p in range(total) if p not in seen]
    if missing:
        raise InvalidPdfOptionError(
            f"order leaves out page(s) {_compact(missing)} of {total}; every page must "
            "appear exactly once. To leave pages out on purpose, use pdf_delete_pages "
            "(or pdf_extract_pages to keep a selection)."
        )
    return order


def _compact(numbers: list[int]) -> str:
    """[1,2,3,7] -> '1-3, 7'"""
    parts: list[str] = []
    start = prev = None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = n
    if start is not None:
        parts.append(f"{start}-{prev}" if prev != start else str(start))
    return ", ".join(parts)


def _select(reader, indices: list[int]) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    # append() keeps bookmarks whose target page survives, pointing at its new place
    writer.append(reader, pages=indices)
    if reader.metadata:
        writer.add_metadata({k: v for k, v in reader.metadata.items() if isinstance(v, str)})
    return _write(writer)


async def delete_pages(data: bytes, pages: str) -> tuple[bytes, list[int], int]:
    """Remove the given pages; returns (pdf, deleted 1-based pages, pages left)"""
    if not pages or not pages.strip():
        raise InvalidPdfOptionError("pages is empty: say which pages to delete, e.g. '2,5-7'.")

    def _run():
        reader = _open(data)
        total = len(reader.pages)
        doomed = set(parse_page_range(pages, total))
        if len(doomed) >= total:
            raise InvalidPdfOptionError(
                f"that would delete all {total} pages; at least one page must remain."
            )
        keep = [i for i in range(total) if i not in doomed]
        return _select(reader, keep), sorted(p + 1 for p in doomed), len(keep)

    return await asyncio.to_thread(_run)


async def reorder_pages(data: bytes, order: str) -> tuple[bytes, list[int]]:
    """Put every page in a new order; returns (pdf, the 1-based order used)"""
    def _run():
        reader = _open(data)
        indices = parse_page_order(order, len(reader.pages))
        return _select(reader, indices), [i + 1 for i in indices]

    return await asyncio.to_thread(_run)


# ----------------------------------------------------------------------------
# Fonts and text
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class _Font:
    name: str
    glyphs: frozenset | None      # None: assume it covers everything (CID fallback)


@lru_cache(maxsize=1)
def _fonts() -> tuple[_Font, ...]:
    """Register the fonts once; returns them in the order they are tried"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    chain: list[_Font] = []
    for name, candidates in (("MorphyardLatin", _LATIN_FONTS), ("MorphyardCJK", _CJK_FONTS)):
        path = next((p for p in candidates if os.path.isfile(p)), None)
        if not path:
            continue
        try:
            font = TTFont(name, path)
            pdfmetrics.registerFont(font)
            chain.append(_Font(name, frozenset(font.face.charToGlyph)))
        except Exception as e:  # a broken font file must not break the tool
            logger.warning(f"[pdf_pages] cannot load font {path}: {e}")
    if not any(f.name == "MorphyardCJK" for f in chain):
        logger.warning(
            "[pdf_pages] no TrueType CJK font found (install fonts-droid-fallback); "
            f"Chinese text uses the non-embedded CID font {_CID_FALLBACK}"
        )
        pdfmetrics.registerFont(UnicodeCIDFont(_CID_FALLBACK))
        chain.append(_Font(_CID_FALLBACK, None))
    return tuple(chain)


def _runs(text: str) -> list[tuple[str, str]]:
    """Split text into (font name, substring) runs, picking per character the
    first font that has a glyph for it. Spaces stay with the run they are in."""
    chain = _fonts()
    runs: list[tuple[str, str]] = []
    for ch in text:
        if ch.isspace() and runs:
            font = runs[-1][0]
        else:
            font = next((f.name for f in chain if f.glyphs is None or ord(ch) in f.glyphs),
                        chain[-1].name)
        if runs and runs[-1][0] == font:
            runs[-1] = (font, runs[-1][1] + ch)
        else:
            runs.append((font, ch))
    return runs


def _width(runs: list[tuple[str, str]], size: float) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    return sum(stringWidth(s, font, size) for font, s in runs)


def _draw(canvas, x: float, y: float, runs: list[tuple[str, str]], size: float) -> None:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    for font, s in runs:
        canvas.setFont(font, size)
        canvas.drawString(x, y, s)
        x += stringWidth(s, font, size)


def _check_text(value: str, what: str) -> str:
    value = (value or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if len(value) > MAX_TEXT_CHARS:
        raise InvalidPdfOptionError(
            f"{what} is {len(value)} characters long; the limit is {MAX_TEXT_CHARS}."
        )
    return value


def parse_color(value: str):
    """'#RGB', '#RRGGBB' or a CSS colour name ('gray', 'red', 'navy'...)

    Parsed by hand: reportlab's own toColor() has a history of evaluating its
    input, so a caller-supplied string never goes near it.
    """
    from reportlab.lib import colors

    v = (value or "").strip().lower()
    m = re.fullmatch(r"#?([0-9a-f]{3}|[0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return colors.Color(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    named = colors.getAllNamedColors().get(v)
    if named is not None:
        return named
    raise InvalidPdfOptionError(
        f"unknown colour {value!r}; use a hex value such as '#808080' or a colour name "
        "such as 'gray', 'red' or 'navy'."
    )


# ----------------------------------------------------------------------------
# Page geometry
# ----------------------------------------------------------------------------

def display_box(page) -> tuple[float, float, tuple]:
    """(width, height, ctm) of the page as a reader sees it

    width/height: the visible (crop) box after the page's /Rotate is applied.
    ctm: maps a point (u, v) of that displayed box, origin bottom-left, to the
    page's own user space: x = a*u + c*v + e, y = b*u + d*v + f.
    """
    box = page.cropbox
    x0, x1 = sorted((float(box.left), float(box.right)))
    y0, y1 = sorted((float(box.bottom), float(box.top)))
    w, h = x1 - x0, y1 - y0
    rotation = (page.get("/Rotate") or 0)
    try:
        rotation = int(rotation) % 360
    except (TypeError, ValueError):
        rotation = 0
    # /Rotate turns the page clockwise for display.
    if rotation == 90:
        return h, w, (0, 1, -1, 0, x0 + w, y0)
    if rotation == 180:
        return w, h, (-1, 0, 0, -1, x0 + w, y0 + h)
    if rotation == 270:
        return h, w, (0, -1, 1, 0, x0, y0 + h)
    return w, h, (1, 0, 0, 1, x0, y0)


def _stamp(data: bytes, targets_of, draw) -> tuple[bytes, int]:
    """Draw an overlay on chosen pages and lay it over them

    targets_of(reader) -> list of 0-based page indices to stamp, in order.
    draw(canvas, width, height, k, n) draws the k-th of n stamped pages in
    displayed coordinates.
    """
    from pypdf import PdfReader, Transformation
    from reportlab.pdfgen.canvas import Canvas

    reader = _open(data)
    targets = targets_of(reader)
    writer = _clone(reader)

    overlay_buf = io.BytesIO()
    # The initial font would otherwise be Helvetica, listed (unused and never
    # embedded) on every stamped page.
    canvas = Canvas(overlay_buf, pageCompression=1, initialFontName=_fonts()[0].name)
    geometry = []
    for k, index in enumerate(targets):
        width, height, ctm = display_box(writer.pages[index])
        canvas.setPageSize((width, height))
        draw(canvas, width, height, k, len(targets))
        canvas.showPage()
        geometry.append(ctm)
    canvas.save()

    overlay = PdfReader(io.BytesIO(overlay_buf.getvalue()))
    for index, ctm, stamp in zip(targets, geometry, overlay.pages):
        writer.pages[index].merge_transformed_page(stamp, Transformation(ctm=ctm))
    return _write(writer), len(targets)


def _page_targets(pages: str, skip_first: bool = False):
    def targets_of(reader) -> list[int]:
        chosen = parse_page_range(pages, len(reader.pages))
        chosen = sorted(set(chosen))
        if skip_first:
            chosen = [i for i in chosen if i != 0]
        if not chosen:
            raise InvalidPdfOptionError(
                "no page is left to stamp (the only chosen page is the skipped first page)."
            )
        return chosen
    return targets_of


# ----------------------------------------------------------------------------
# Watermark
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class WatermarkImage:
    image: object          # PIL.Image.Image, already loaded
    width: int
    height: int


def load_watermark_image(blob: bytes) -> WatermarkImage:
    """Decode and check a PNG or JPEG watermark image"""
    from PIL import Image

    if len(blob) > MAX_WATERMARK_IMAGE_MB * 1024 * 1024:
        raise InvalidPdfOptionError(
            f"the watermark image is {len(blob) / 1024 / 1024:.1f} MB; the limit is "
            f"{MAX_WATERMARK_IMAGE_MB} MB."
        )
    try:
        img = Image.open(io.BytesIO(blob))
        fmt = img.format
    except Exception:
        raise InvalidPdfOptionError("watermark_image is not a PNG or JPEG image.")
    if fmt not in ("PNG", "JPEG"):
        raise InvalidPdfOptionError(
            f"watermark_image must be PNG or JPEG; got {fmt or 'an unknown format'}."
        )
    if img.width * img.height > MAX_WATERMARK_IMAGE_PIXELS:
        raise InvalidPdfOptionError(
            f"the watermark image is {img.width}x{img.height} pixels; the limit is "
            f"{MAX_WATERMARK_IMAGE_PIXELS // 1_000_000} megapixels."
        )
    try:
        img.load()
    except Exception as e:
        raise InvalidPdfOptionError(f"the watermark image cannot be decoded: {e}")
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info
                          else "RGB")
    return WatermarkImage(img, img.width, img.height)


@dataclass(frozen=True)
class WatermarkOptions:
    text: str = ""
    image: WatermarkImage | None = None
    position: str = "center"
    rotation: float = 45
    font_size: float = 48
    opacity: float = 0.3
    color: str = "#808080"
    image_scale: float = 0.5
    pages: str = ""


def validate_watermark(opts: WatermarkOptions) -> None:
    has_text = bool(opts.text and opts.text.strip())
    if has_text == (opts.image is not None):
        raise InvalidPdfOptionError(
            "give exactly one of text or watermark_image (a text watermark or an image "
            "watermark, not both and not neither)."
        )
    if has_text:
        _check_text(opts.text, "text")
    if opts.position not in WATERMARK_POSITIONS:
        raise InvalidPdfOptionError(
            f"position must be one of {', '.join(WATERMARK_POSITIONS)}; got {opts.position!r}."
        )
    if not -360 <= opts.rotation <= 360:
        raise InvalidPdfOptionError("rotation must be between -360 and 360 degrees.")
    if not 6 <= opts.font_size <= 300:
        raise InvalidPdfOptionError("font_size must be between 6 and 300 points.")
    if not 0.05 <= opts.opacity <= 1:
        raise InvalidPdfOptionError("opacity must be between 0.05 and 1.")
    if not 0.05 <= opts.image_scale <= 1:
        raise InvalidPdfOptionError("image_scale must be between 0.05 and 1.")
    parse_color(opts.color)


def _watermark_drawer(opts: WatermarkOptions):
    from reportlab.lib.utils import ImageReader

    color = parse_color(opts.color)
    runs = _runs(_check_text(opts.text, "text")) if opts.image is None else None
    image_reader = ImageReader(opts.image.image) if opts.image is not None else None

    def draw(canvas, width, height, _k, _n):
        if opts.position == "diagonal":
            angle = math.degrees(math.atan2(height, width))
        else:
            angle = opts.rotation
        canvas.saveState()
        canvas.setFillColor(color)
        canvas.setFillAlpha(opts.opacity)
        canvas.setStrokeAlpha(opts.opacity)

        if runs is not None:
            size = opts.font_size
            item_w = _width(runs, size)
            if opts.position != "tiled":
                # Keep a single watermark on the page: shrink a long text to fit
                # the line it is drawn along.
                room = 0.9 * (math.hypot(width, height) if opts.position == "diagonal"
                              else _span(width, height, angle))
                if item_w > room > 0:
                    size = max(MIN_FIT_FONT_SIZE, size * room / item_w)
                    item_w = _width(runs, size)
            item_h = size

            def paint(cx, cy):
                _draw(canvas, cx - item_w / 2, cy - size * 0.35, runs, size)
        else:
            img = opts.image
            scale = min(width * opts.image_scale / img.width,
                        height * opts.image_scale / img.height)
            item_w, item_h = img.width * scale, img.height * scale

            def paint(cx, cy):
                canvas.drawImage(image_reader, cx - item_w / 2, cy - item_h / 2,
                                 item_w, item_h, mask="auto")

        canvas.translate(width / 2, height / 2)
        canvas.rotate(angle)
        if opts.position == "tiled":
            for cx, cy in _tile_centres(width, height, item_w, item_h):
                paint(cx, cy)
        else:
            paint(0, 0)
        canvas.restoreState()

    return draw


def _span(width: float, height: float, angle: float) -> float:
    """Length of the longest line at `angle` through the centre that stays on the page"""
    a = math.radians(angle)
    cos, sin = abs(math.cos(a)), abs(math.sin(a))
    limits = []
    if cos > 1e-9:
        limits.append(width / cos)
    if sin > 1e-9:
        limits.append(height / sin)
    return min(limits)


def _tile_centres(width, height, item_w, item_h) -> list[tuple[float, float]]:
    """Centres of a staggered grid that covers the page whatever the rotation"""
    reach = math.hypot(width, height) / 2 + max(item_w, item_h)
    step_x = item_w + max(item_h * 2, 36)
    step_y = item_h * 3 + 24
    count = (2 * reach / step_x + 1) * (2 * reach / step_y + 1)
    if count > MAX_TILES_PER_PAGE:
        stretch = math.sqrt(count / MAX_TILES_PER_PAGE)
        step_x, step_y = step_x * stretch, step_y * stretch
    centres = []
    row = 0
    y = -reach
    while y <= reach:
        x = -reach + (step_x / 2 if row % 2 else 0)
        while x <= reach:
            centres.append((x, y))
            x += step_x
        y += step_y
        row += 1
    return centres


async def watermark_pdf(data: bytes, opts: WatermarkOptions) -> tuple[bytes, int]:
    """Lay a text or image watermark over the chosen pages; returns (pdf, pages stamped)"""
    validate_watermark(opts)

    def _run():
        return _stamp(data, _page_targets(opts.pages), _watermark_drawer(opts))

    result, stamped = await asyncio.to_thread(_run)
    logger.info(f"[pdf_pages] watermark on {stamped} page(s) → {len(result)} bytes")
    return result, stamped


# ----------------------------------------------------------------------------
# Page numbers, header and footer
# ----------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _check_template(value: str, what: str) -> str:
    value = _check_text(value, what)
    for name in _PLACEHOLDER.findall(value):
        if name not in ("page", "total"):
            raise InvalidPdfOptionError(
                f"{what} uses {{{name}}}; the only placeholders are {{page}} and {{total}}."
            )
    return value


def _fill(template: str, page: int, total: int) -> str:
    # Plain replacement, never str.format: the template comes from the caller.
    return template.replace("{page}", str(page)).replace("{total}", str(total))


@dataclass(frozen=True)
class PageNumberOptions:
    number_format: str = "{page}"
    number_position: str = "bottom-center"
    start_number: int = 1
    skip_first_page: bool = False
    pages: str = ""
    header_text: str = ""
    header_align: str = "center"
    footer_text: str = ""
    footer_align: str = "left"
    font_size: float = 10
    margin: float = 28
    color: str = "#000000"


def validate_page_numbers(opts: PageNumberOptions) -> None:
    number = _check_template(opts.number_format, "number_format").strip()
    header = _check_template(opts.header_text, "header_text").strip()
    footer = _check_template(opts.footer_text, "footer_text").strip()
    if not (number or header or footer):
        raise InvalidPdfOptionError(
            "nothing to add: give a number_format, a header_text or a footer_text."
        )
    if opts.number_position not in NUMBER_POSITIONS:
        raise InvalidPdfOptionError(
            f"number_position must be one of {', '.join(NUMBER_POSITIONS)}; "
            f"got {opts.number_position!r}."
        )
    for what, value in (("header_align", opts.header_align),
                        ("footer_align", opts.footer_align)):
        if value not in ALIGNMENTS:
            raise InvalidPdfOptionError(
                f"{what} must be one of {', '.join(ALIGNMENTS)}; got {value!r}."
            )
    if number:
        clash = (("header", header, f"top-{opts.header_align}"),
                 ("footer", footer, f"bottom-{opts.footer_align}"))
        for what, text, spot in clash:
            if text and spot == opts.number_position:
                raise InvalidPdfOptionError(
                    f"the page number and the {what} would overlap at {spot}. Move one "
                    f"of them, or put {{page}} into {what}_text and leave number_format "
                    "empty."
                )
    if not -100000 <= opts.start_number <= 1000000:
        raise InvalidPdfOptionError("start_number must be between -100000 and 1000000.")
    if not 6 <= opts.font_size <= 72:
        raise InvalidPdfOptionError("font_size must be between 6 and 72 points.")
    if not 0 <= opts.margin <= 200:
        raise InvalidPdfOptionError("margin must be between 0 and 200 points.")
    parse_color(opts.color)


def _page_number_drawer(opts: PageNumberOptions):
    color = parse_color(opts.color)
    number = opts.number_format.strip()
    header = opts.header_text.strip()
    footer = opts.footer_text.strip()
    items = []
    if number:
        vertical, horizontal = opts.number_position.split("-")
        items.append((number, vertical, horizontal))
    if header:
        items.append((header, "top", opts.header_align))
    if footer:
        items.append((footer, "bottom", opts.footer_align))

    def draw(canvas, width, height, k, n):
        page_no = opts.start_number + k
        last_no = opts.start_number + n - 1
        # Keep the text on the page even when the margin is large for its size
        margin = min(opts.margin, width / 3, height / 3)
        canvas.saveState()
        canvas.setFillColor(color)
        for template, vertical, horizontal in items:
            runs = _runs(_fill(template, page_no, last_no))
            size = opts.font_size
            text_w = _width(runs, size)
            room = width - 2 * margin
            if text_w > room > 0:
                size = max(MIN_FIT_FONT_SIZE, size * room / text_w)
                text_w = _width(runs, size)
            if horizontal == "left":
                x = margin
            elif horizontal == "right":
                x = width - margin - text_w
            else:
                x = (width - text_w) / 2
            y = margin if vertical == "bottom" else height - margin - size * 0.8
            _draw(canvas, x, y, runs, size)
        canvas.restoreState()

    return draw


async def add_page_numbers(data: bytes, opts: PageNumberOptions) -> tuple[bytes, int]:
    """Add page numbers and/or a header and footer; returns (pdf, pages stamped)"""
    validate_page_numbers(opts)

    def _run():
        return _stamp(data, _page_targets(opts.pages, opts.skip_first_page),
                      _page_number_drawer(opts))

    result, stamped = await asyncio.to_thread(_run)
    logger.info(f"[pdf_pages] page numbers on {stamped} page(s) → {len(result)} bytes")
    return result, stamped
