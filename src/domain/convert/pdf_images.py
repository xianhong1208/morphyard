"""PDF pages to images, and images to a PDF

Two operations that sit next to the route table rather than inside it:

- **pdf -> images** produces one file per page, which the (src, dst) route table
  cannot express (every route yields exactly one file). Pages are rendered by
  Poppler's `pdftoppm`, already installed for OCR, one subprocess per page so each
  page can get its own resolution under the pixel cap.
- **images -> pdf** takes one image (a multi-page TIFF gives several pages) or a
  ZIP of images, because the host injects only one file per call (see the note on
  pdf_merge in morphyard_tools.py). The single-image case is also registered as a
  route (`ImagesToPdfEngine`), so convert_document png -> pdf builds a real PDF
  instead of OCRing the picture into text.

The PDF is written by hand rather than with Pillow's PDF writer: Pillow re-encodes
every JPEG and only makes pages the size of the image. Writing the few objects an
image PDF needs keeps JPEGs byte for byte (EXIF orientation is applied with the
placement matrix instead of by rotating pixels) and allows A4 / Letter pages with
margins. No new dependency.
"""

import asyncio
import io
import math
import re
import zipfile
import zlib
from dataclasses import dataclass, field

from src.domain.convert.engines import base
from src.domain.convert.engines.base import CliEngine, ConversionEngine, Workspace
from src.domain.convert.engines.discovery import find_binary
from src.domain.convert.pdfops import parse_page_range
from src.domain.convert.types import ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# ============================================================================
# PDF -> images limits
# ============================================================================

DEFAULT_DPI = 150
MIN_DPI = 36
MAX_DPI = 300
# Pixels per rendered page. A4 at 300 DPI is 8.7 MP and A3 17.4 MP; a poster-sized
# page at the requested DPI would be hundreds of MP, so its DPI is lowered instead.
MAX_PAGE_PIXELS = 25_000_000
# Pages per call, the same bound as pdf_split's 50 files.
MAX_RENDER_PAGES = 50
# Total size of the rendered images. The result travels back as base64 inside the
# MCP response (download links come in 1.4).
MAX_RENDER_OUTPUT_BYTES = 50 * 1024 * 1024
RENDER_FORMATS = ("png", "jpg", "webp")
DEFAULT_QUALITY = 85

# ============================================================================
# Images -> PDF limits
# ============================================================================

MAX_PDF_PAGES = 100                       # images (TIFF frames count one each)
MAX_IMAGE_PIXELS = 50_000_000             # one image; a 48 MP phone photo fits
MAX_TOTAL_PIXELS = 400_000_000            # all images together
MAX_ZIP_ENTRIES = 500                     # entries in the archive, images or not
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024  # all entries unpacked together
MAX_PDF_OUTPUT_BYTES = 100 * 1024 * 1024
PAGE_SIZES = {"a4": (595.276, 841.89), "letter": (612.0, 792.0)}
PAGE_SIZE_CHOICES = ("fit", "a4", "letter")
ORIENTATIONS = ("auto", "portrait", "landscape")
DEFAULT_MARGIN_MM = 10.0
MAX_MARGIN_MM = 50.0
# Image resolution assumed for "fit" pages when the file records none.
DEFAULT_IMAGE_DPI = 96.0
# The largest page Acrobat opens (200 inches).
MAX_PAGE_POINTS = 14400.0
MIN_PAGE_POINTS = 3.0

# Pillow formats accepted as input. MPO is what many cameras write for a JPEG.
_ACCEPTED_IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "TIFF", "GIF", "BMP"}
_ZIP_MAGIC = b"PK\x03\x04"


def _render_error(dst: str, reason: str) -> ConversionFailedError:
    return ConversionFailedError("pdftoppm", "pdf", dst, reason)


def _pdf_error(src: str, reason: str) -> ConversionFailedError:
    return ConversionFailedError("images-to-pdf", src, "pdf", reason)


# ============================================================================
# PDF -> images
# ============================================================================

@dataclass
class RenderedPages:
    """Rendered pages: [(1-based page number, image bytes)], plus notes for the caller"""
    fmt: str
    dpi: int
    images: list[tuple[int, bytes]] = field(default_factory=list)
    lowered: dict[int, int] = field(default_factory=dict)   # page -> DPI actually used
    total_pages: int = 0


class _Pdftoppm(CliEngine):
    """Poppler's pdftoppm, used for its subprocess handling (timeout, process group)"""

    name = "pdftoppm"
    binary = "pdftoppm"

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        raise ConversionFailedError(self.name, src, dst, "use render_pages()")


_PDFTOPPM = _Pdftoppm()


def normalize_render_format(value: str | None) -> str:
    token = (value or "png").strip().lower().lstrip(".")
    token = {"jpeg": "jpg"}.get(token, token)
    if token not in RENDER_FORMATS:
        raise _render_error(
            token, f"image_format must be one of {', '.join(RENDER_FORMATS)}, got '{value}'",
        )
    return token


def page_dpi(width_pt: float, height_pt: float, dpi: int,
             max_pixels: int = MAX_PAGE_PIXELS) -> int:
    """The DPI to render a page at: the requested one, lowered until it fits max_pixels"""
    pixels = (width_pt / 72 * dpi) * (height_pt / 72 * dpi)
    if pixels <= max_pixels:
        return dpi
    fitted = int(math.floor(72 * math.sqrt(max_pixels / (width_pt * height_pt))))
    return max(1, min(dpi, fitted))


def _page_plan(data: bytes, page_spec: str, dst: str) -> tuple[int, list[tuple[int, float, float]]]:
    """Read the page sizes with pypdf: (total pages, [(page number, width pt, height pt)])"""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise _render_error(
                dst,
                "this PDF is password-protected. Ask the user for the password and "
                "unlock it first; it cannot be rendered while it is encrypted.",
            )
        total = len(reader.pages)
        indices = parse_page_range(page_spec, total)
        if len(indices) > MAX_RENDER_PAGES:
            raise _render_error(
                dst,
                f"{len(indices)} pages selected; at most {MAX_RENDER_PAGES} pages can be "
                f"rendered per call. Call again with a smaller pages range, e.g. "
                f"pages='1-{MAX_RENDER_PAGES}', then '{MAX_RENDER_PAGES + 1}-"
                f"{min(total, 2 * MAX_RENDER_PAGES)}'.",
            )
        sizes = []
        for i in indices:
            page = reader.pages[i]
            unit = float(page.user_unit or 1)
            box = page.cropbox
            sizes.append((i + 1, abs(float(box.width)) * unit, abs(float(box.height)) * unit))
        return total, sizes
    except ConversionFailedError as e:
        # parse_page_range raises with engine "pypdf"; keep its reason under our name
        if e.details.get("engine") == "pypdf":
            raise _render_error(dst, e.details["reason"])
        raise
    except Exception as e:
        raise _render_error(dst, f"not a readable PDF: {str(e)[:300]}")


def validate_render_args(fmt: str | None, dpi: int, quality: int) -> tuple[str, int, int]:
    """Check the options before a conversion slot is taken: (format, dpi, quality)"""
    fmt = normalize_render_format(fmt)
    if not MIN_DPI <= int(dpi) <= MAX_DPI:
        raise _render_error(fmt, f"dpi must be between {MIN_DPI} and {MAX_DPI}, got {dpi}")
    if not 1 <= int(quality) <= 95:
        raise _render_error(fmt, f"quality must be between 1 and 95, got {quality}")
    return fmt, int(dpi), int(quality)


async def render_pages(data: bytes, fmt: str = "png", dpi: int = DEFAULT_DPI,
                       page_spec: str = "", quality: int = DEFAULT_QUALITY) -> RenderedPages:
    """Render the selected pages of a PDF to images, one image per page, in the order given"""
    fmt, dpi, quality = validate_render_args(fmt, dpi, quality)

    # The PDF and the page range are checked first, so their errors are the ones reported
    total, plan = await asyncio.to_thread(_page_plan, data, page_spec, fmt)
    pdftoppm = find_binary("pdftoppm")
    if not pdftoppm:
        raise _render_error(fmt, "Poppler (pdftoppm) is not installed on this server")
    result = RenderedPages(fmt=fmt, dpi=dpi, total_pages=total)
    timeout = base.resolve_timeout({}, 120)
    produced = 0

    with Workspace() as ws:
        ws.write("input.pdf", data)
        for number, width_pt, height_pt in plan:
            used = page_dpi(width_pt, height_pt, dpi)
            if used != dpi:
                result.lowered[number] = used
            argv = [pdftoppm, "-f", str(number), "-l", str(number), "-r", str(used),
                    "-singlefile"]
            if fmt == "jpg":
                argv += ["-jpeg", "-jpegopt", f"quality={quality}"]
                out_name = "page.jpg"
            else:
                argv += ["-png"]
                out_name = "page.png"
            argv += ["input.pdf", "page"]
            await _PDFTOPPM._run_command(argv, cwd=ws.path, src="pdf", dst=fmt,
                                         timeout=timeout)
            try:
                blob = ws.read(out_name)
            except FileNotFoundError:
                raise _render_error(fmt, f"pdftoppm produced no image for page {number}")
            (ws.path / out_name).unlink()
            if fmt == "webp":
                blob = await asyncio.to_thread(_png_to_webp, blob, quality)

            produced += len(blob)
            if produced > MAX_RENDER_OUTPUT_BYTES:
                raise _render_error(
                    fmt,
                    f"the images would be larger than "
                    f"{MAX_RENDER_OUTPUT_BYTES // (1024 * 1024)} MB in total. Use a lower "
                    f"dpi, image_format='jpg', or fewer pages per call.",
                )
            result.images.append((number, blob))

    logger.info(
        f"[pdftoppm] rendered {len(result.images)} page(s) as {fmt} at {dpi} DPI, "
        f"{produced} bytes"
    )
    return result


def _png_to_webp(blob: bytes, quality: int) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(blob)) as img:
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=quality)
        return out.getvalue()


def zip_images(entries: list[tuple[str, bytes]]) -> bytes:
    """Pack (name, bytes) into a ZIP. Stored, not deflated: the images are already compressed."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, blob in entries:
            archive.writestr(name, blob)
    return buffer.getvalue()


# ============================================================================
# Images -> PDF: reading the input
# ============================================================================

@dataclass
class _Picture:
    """One page's image, ready to be embedded"""
    label: str
    width: int                # stored pixel size
    height: int
    color_space: str          # /DeviceRGB, /DeviceGray
    components: int
    filter: str               # /DCTDecode or /FlateDecode
    data: bytes
    orientation: int = 1      # EXIF orientation still to apply (JPEG pass-through only)
    dpi: tuple[float, float] | None = None
    icc: bytes | None = None

    @property
    def display_size(self) -> tuple[int, int]:
        if self.orientation in (5, 6, 7, 8):
            return self.height, self.width
        return self.width, self.height


@dataclass
class _Collected:
    pictures: list[_Picture] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    passed_through: int = 0
    total_pixels: int = 0


def _natural_key(name: str):
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", name)]


def _zip_members(data: bytes, src: str) -> list[tuple[str, bytes]]:
    """Image candidates of a ZIP, in natural name order (page2 before page10)"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise _pdf_error(src, f"the ZIP archive cannot be read: {e}")
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise _pdf_error(
                src, f"the ZIP has {len(infos)} entries; at most {MAX_ZIP_ENTRIES} are read",
            )
        members = []
        for info in infos:
            name = info.filename
            base_name = name.rsplit("/", 1)[-1]
            if info.is_dir() or name.startswith("__MACOSX/") or base_name.startswith("."):
                continue
            members.append(info)
        members.sort(key=lambda i: _natural_key(i.filename))

        out: list[tuple[str, bytes]] = []
        unpacked = 0
        for info in members:
            if info.flag_bits & 0x1:
                raise _pdf_error(src, f"'{info.filename}' in the ZIP is password-protected")
            # file_size comes from the archive and can lie, so the read itself is bounded
            budget = MAX_ZIP_UNCOMPRESSED - unpacked
            try:
                with archive.open(info) as handle:
                    blob = handle.read(budget + 1)
            except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError,
                    EOFError) as e:
                raise _pdf_error(src, f"'{info.filename}' in the ZIP cannot be read: {e}")
            unpacked += len(blob)
            if unpacked > MAX_ZIP_UNCOMPRESSED:
                raise _pdf_error(src, _zip_too_big())
            out.append((info.filename, blob))
        return out


def _zip_too_big() -> str:
    return (f"the ZIP unpacks to more than {MAX_ZIP_UNCOMPRESSED // (1024 * 1024)} MB; "
            f"split the images over several calls")


def _flatten(img):
    """Any mode -> RGB or L, transparency composited onto white"""
    from PIL import Image

    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if img.mode in ("1", "L"):
        return img.convert("L")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _dpi_of(img) -> tuple[float, float] | None:
    dpi = img.info.get("dpi")
    try:
        x, y = float(dpi[0]), float(dpi[1])
    except (TypeError, ValueError, IndexError):
        return None
    if 10 <= x <= 4800 and 10 <= y <= 4800:
        return x, y
    return None


def _exif_orientation(img) -> int:
    try:
        value = int(img.getexif().get(0x0112, 1))
    except Exception:
        return 1
    return value if 1 <= value <= 8 else 1


def _encode_flate(frame, label: str, dpi, icc) -> _Picture:
    from PIL import ImageOps

    frame = ImageOps.exif_transpose(frame)
    frame = _flatten(frame)
    gray = frame.mode == "L"
    return _Picture(
        label=label, width=frame.width, height=frame.height,
        color_space="/DeviceGray" if gray else "/DeviceRGB",
        components=1 if gray else 3, filter="/FlateDecode",
        data=zlib.compress(frame.tobytes(), 6), dpi=dpi,
        icc=_icc_for(icc, 1 if gray else 3),
    )


def _encode_jpeg(frame, label: str, dpi, icc) -> _Picture:
    """Re-encode as JPEG (camera MPO files and CMYK JPEGs): a lossy source stays lossy"""
    from PIL import ImageOps

    frame = _flatten(ImageOps.exif_transpose(frame))
    out = io.BytesIO()
    frame.save(out, format="JPEG", quality=95)
    gray = frame.mode == "L"
    return _Picture(
        label=label, width=frame.width, height=frame.height,
        color_space="/DeviceGray" if gray else "/DeviceRGB",
        components=1 if gray else 3, filter="/DCTDecode", data=out.getvalue(),
        dpi=dpi, icc=_icc_for(icc, 1 if gray else 3),
    )


def _icc_for(icc: bytes | None, components: int) -> bytes | None:
    """The ICC profile, only if it describes the image's colour space (else it is dropped)"""
    if not icc or len(icc) < 132:
        return None
    space = icc[16:20]
    if (components == 3 and space == b"RGB ") or (components == 1 and space == b"GRAY"):
        return icc
    return None


def _jpeg_bytes(img, blob: bytes) -> bytes | None:
    """The JPEG stream to embed unchanged, or None when the image must be re-encoded

    Plain RGB / grayscale JPEGs go in as they are. A camera MPO file is the main
    JPEG followed by extra images (a preview, a depth map); its first image is cut
    out by the size the MP index records. CMYK JPEGs are re-encoded: whether their
    samples are stored inverted depends on the writer.
    """
    if img.mode not in ("RGB", "L"):
        return None
    if img.format == "JPEG":
        return blob
    if img.format == "MPO":
        try:
            first = img.mpinfo[0xB002][0]["Size"]
        except Exception:
            return None
        part = blob[:first]
        if part.startswith(b"\xff\xd8") and part.rstrip(b"\x00").endswith(b"\xff\xd9"):
            return part
    return None


def _add_image(collected: _Collected, label: str, blob: bytes, src: str,
               from_zip: bool) -> None:
    from PIL import Image, ImageSequence, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(blob))
    except UnidentifiedImageError:
        if from_zip:
            collected.skipped.append(label)
            return
        what = "this file" if label == "image" else f"'{label}'"
        raise _pdf_error(
            src, f"{what} is not an image this tool can read (PNG, JPEG, WebP, TIFF, "
                 "GIF, BMP, or a ZIP of them)",
        )
    except Image.DecompressionBombError:
        raise _pdf_error(src, _too_many_pixels(label))
    except Exception as e:
        raise _pdf_error(src, f"'{label}' cannot be read as an image: {str(e)[:200]}")

    with img:
        if img.format not in _ACCEPTED_IMAGE_FORMATS:
            if from_zip:
                collected.skipped.append(label)
                return
            raise _pdf_error(
                src, f"{img.format} images are not supported; use PNG, JPEG, WebP, TIFF, "
                     f"GIF or BMP",
            )

        # Multi-page TIFF: every frame is a page. GIF / WebP animations and MPO
        # (a JPEG plus a preview) contribute their first frame only.
        frames = getattr(img, "n_frames", 1) if img.format == "TIFF" else 1
        # Checked before any frame is decoded: a 1000-page TIFF is refused at once
        if len(collected.pictures) + frames > MAX_PDF_PAGES:
            raise _pdf_error(src, _too_many_pages())

        icc = img.info.get("icc_profile") or None
        dpi = _dpi_of(img)
        iterator = ImageSequence.Iterator(img) if frames > 1 else [img]
        for index, frame in enumerate(iterator):
            if index >= frames:
                break
            name = label if frames == 1 else f"{label} (page {index + 1})"
            if frame.width * frame.height > MAX_IMAGE_PIXELS:
                raise _pdf_error(src, _too_many_pixels(name))
            collected.total_pixels += frame.width * frame.height
            if collected.total_pixels > MAX_TOTAL_PIXELS:
                raise _pdf_error(
                    src, f"the images hold more than {MAX_TOTAL_PIXELS // 1_000_000} "
                         f"megapixels together; split them over several calls",
                )
            frame_dpi = _dpi_of(frame) or dpi
            jpeg = _jpeg_bytes(img, blob)
            if jpeg is not None:
                # Byte for byte: no decode, no quality loss. Orientation is applied
                # when the image is placed.
                components = 1 if img.mode == "L" else 3
                collected.pictures.append(_Picture(
                    label=name, width=img.width, height=img.height,
                    color_space="/DeviceGray" if components == 1 else "/DeviceRGB",
                    components=components, filter="/DCTDecode",
                    data=jpeg, orientation=_exif_orientation(img), dpi=frame_dpi,
                    icc=_icc_for(icc, components),
                ))
                collected.passed_through += 1
            elif img.format in ("JPEG", "MPO"):
                collected.pictures.append(_encode_jpeg(frame, name, frame_dpi, icc))
            else:
                frame_icc = frame.info.get("icc_profile") or icc
                collected.pictures.append(_encode_flate(frame, name, frame_dpi, frame_icc))


def _too_many_pixels(label: str) -> str:
    return (f"'{label}' has more than {MAX_IMAGE_PIXELS // 1_000_000} megapixels; scale it "
            f"down first")


def _too_many_pages() -> str:
    return (f"more than {MAX_PDF_PAGES} images (TIFF pages count one each); split them "
            f"over several calls")


def collect_images(data: bytes | list[tuple[str, bytes]], src: str = "image") -> _Collected:
    """Read one image, a multi-page TIFF, a ZIP of images, or a list of (label, image) into
    pages. In a list every item must be an image (a ZIP is refused, naming the item): one
    call never unpacks several archives."""
    collected = _Collected()
    if isinstance(data, list):
        for label, blob in data:
            if blob.startswith(_ZIP_MAGIC):
                raise _pdf_error(
                    src, f"'{label}' is a ZIP; pass the images themselves when giving several "
                         f"files, or the ZIP alone",
                )
            _add_image(collected, label, blob, src, from_zip=False)
    elif data.startswith(_ZIP_MAGIC):
        for name, blob in _zip_members(data, src):
            _add_image(collected, name, blob, src, from_zip=True)
        if not collected.pictures:
            raise _pdf_error(
                src, "the ZIP contains no images this tool can read (PNG, JPEG, WebP, "
                     "TIFF, GIF, BMP)",
            )
    else:
        _add_image(collected, "image", data, src, from_zip=False)
    return collected


# ============================================================================
# Images -> PDF: layout and writing
# ============================================================================

def _num(value: float) -> bytes:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return (text if text not in ("", "-0") else "0").encode()


def _placement_matrix(orientation: int, w: float, h: float, x: float, y: float) -> list[float]:
    """`cm` operands drawing a stored image so it appears upright in the box (x, y, w, h)

    An image XObject paints the unit square with its first stored row at the top.
    For each EXIF orientation the stored pixel (u, v) (0..1, v downwards) appears at
    (s, t) of the upright picture; solving page = matrix x unit-square gives these.
    """
    return {
        1: [w, 0, 0, h, x, y],
        2: [-w, 0, 0, h, x + w, y],
        3: [-w, 0, 0, -h, x + w, y + h],
        4: [w, 0, 0, -h, x, y + h],
        5: [0, -h, -w, 0, x + w, y + h],
        6: [0, -h, w, 0, x, y + h],
        7: [0, h, w, 0, x, y],
        8: [0, h, -w, 0, x + w, y],
    }[orientation]


def _page_layout(picture: _Picture, page_size: str, orientation: str,
                 margin_pt: float) -> tuple[float, float, float, float, float, float]:
    """(page width, page height, image x, y, width, height) in points"""
    disp_w, disp_h = picture.display_size
    if page_size == "fit":
        dpi_x, dpi_y = picture.dpi or (DEFAULT_IMAGE_DPI, DEFAULT_IMAGE_DPI)
        if picture.orientation in (5, 6, 7, 8):
            dpi_x, dpi_y = dpi_y, dpi_x
        img_w, img_h = disp_w / dpi_x * 72, disp_h / dpi_y * 72
        largest = MAX_PAGE_POINTS - 2 * margin_pt
        scale = min(1.0, largest / img_w, largest / img_h)
        img_w, img_h = max(MIN_PAGE_POINTS, img_w * scale), max(MIN_PAGE_POINTS, img_h * scale)
        return (img_w + 2 * margin_pt, img_h + 2 * margin_pt,
                margin_pt, margin_pt, img_w, img_h)

    short, long_ = PAGE_SIZES[page_size]
    landscape = (orientation == "landscape"
                 or (orientation == "auto" and disp_w > disp_h))
    page_w, page_h = (long_, short) if landscape else (short, long_)
    box_w, box_h = page_w - 2 * margin_pt, page_h - 2 * margin_pt
    scale = min(box_w / disp_w, box_h / disp_h)
    img_w, img_h = disp_w * scale, disp_h * scale
    return (page_w, page_h, (page_w - img_w) / 2, (page_h - img_h) / 2, img_w, img_h)


def _write_pdf(pages: list[tuple[float, float, list[float], _Picture]]) -> bytes:
    """A minimal PDF: one image XObject per page, drawn with one `cm` + `Do`"""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    def stream(entries: bytes, payload: bytes) -> bytes:
        return (b"<< " + entries + b" /Length " + str(len(payload)).encode() + b" >>\nstream\n"
                + payload + b"\nendstream")

    add(b"")  # 1: catalog, filled in last
    add(b"")  # 2: page tree, filled in last
    kids = []
    for page_w, page_h, matrix, pic in pages:
        color = pic.color_space.encode()
        if pic.icc:
            alternate = color
            icc_id = add(stream(b"/N " + str(pic.components).encode()
                                + b" /Alternate " + alternate, pic.icc))
            color = f"[/ICCBased {icc_id} 0 R]".encode()
        image_id = add(stream(
            b"/Type /XObject /Subtype /Image /Width " + str(pic.width).encode()
            + b" /Height " + str(pic.height).encode() + b" /ColorSpace " + color
            + b" /BitsPerComponent 8 /Filter " + pic.filter.encode(),
            pic.data,
        ))
        content = b"q " + b" ".join(_num(v) for v in matrix) + b" cm /Im0 Do Q"
        content_id = add(stream(b"", content))
        page_id = add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + _num(page_w) + b" "
            + _num(page_h) + b"] /Resources << /XObject << /Im0 "
            + str(image_id).encode() + b" 0 R >> >> /Contents "
            + str(content_id).encode() + b" 0 R >>"
        )
        kids.append(page_id)

    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (b"<< /Type /Pages /Kids [" + b" ".join(f"{k} 0 R".encode() for k in kids)
                  + b"] /Count " + str(len(kids)).encode() + b" >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref_at}\n%%EOF\n".encode())
    return out.getvalue()


@dataclass
class BuiltPdf:
    data: bytes
    pages: int
    passed_through: int
    skipped: list[str]
    labels: list[str]


def validate_layout(page_size: str, orientation: str, margin_mm: float | None,
                     src: str) -> tuple[str, str, float]:
    page_size = (page_size or "fit").strip().lower()
    if page_size not in PAGE_SIZE_CHOICES:
        raise _pdf_error(src, f"page_size must be one of {', '.join(PAGE_SIZE_CHOICES)}, "
                              f"got '{page_size}'")
    orientation = (orientation or "auto").strip().lower()
    if orientation not in ORIENTATIONS:
        raise _pdf_error(src, f"orientation must be one of {', '.join(ORIENTATIONS)}, "
                              f"got '{orientation}'")
    if margin_mm is None:
        margin_mm = 0.0 if page_size == "fit" else DEFAULT_MARGIN_MM
    margin_mm = float(margin_mm)
    if not 0 <= margin_mm <= MAX_MARGIN_MM:
        raise _pdf_error(src, f"margin_mm must be between 0 and {MAX_MARGIN_MM:g}, "
                              f"got {margin_mm:g}")
    return page_size, orientation, margin_mm


def build_pdf_sync(data: bytes | list[tuple[str, bytes]], page_size: str = "fit",
                   orientation: str = "auto",
                   margin_mm: float | None = None, src: str = "image") -> BuiltPdf:
    page_size, orientation, margin_mm = validate_layout(page_size, orientation, margin_mm, src)
    margin_pt = margin_mm / 25.4 * 72
    collected = collect_images(data, src)

    pages = []
    size = 0
    for pic in collected.pictures:
        page_w, page_h, x, y, w, h = _page_layout(pic, page_size, orientation, margin_pt)
        pages.append((page_w, page_h, _placement_matrix(pic.orientation, w, h, x, y), pic))
        size += len(pic.data) + len(pic.icc or b"")
        if size > MAX_PDF_OUTPUT_BYTES:
            raise _pdf_error(
                src, f"the PDF would be larger than {MAX_PDF_OUTPUT_BYTES // (1024 * 1024)} MB; "
                     f"split the images over several calls or use JPEG images",
            )
    return BuiltPdf(
        data=_write_pdf(pages), pages=len(pages), passed_through=collected.passed_through,
        skipped=collected.skipped, labels=[p.label for p in collected.pictures],
    )


async def build_pdf(data: bytes | list[tuple[str, bytes]], page_size: str = "fit",
                    orientation: str = "auto",
                    margin_mm: float | None = None, src: str = "image") -> BuiltPdf:
    result = await asyncio.to_thread(build_pdf_sync, data, page_size, orientation,
                                     margin_mm, src)
    logger.info(
        f"[images-to-pdf] {result.pages} page(s), {result.passed_through} JPEG(s) kept "
        f"as is, {len(result.data)} bytes"
    )
    return result


class ImagesToPdfEngine(ConversionEngine):
    """Route-table engine for a single image (or multi-page TIFF) -> pdf, page = image size"""

    name = "images-to-pdf"

    def is_available(self) -> bool:
        try:
            import PIL  # noqa: F401
            return True
        except ImportError:
            return False

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        if dst != "pdf":
            raise ConversionFailedError(self.name, src, dst, "only produces PDF")
        return (await build_pdf(data, src=src)).data
