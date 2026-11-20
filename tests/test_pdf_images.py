"""PDF pages -> images (pdf_to_images) and images -> PDF (images_to_pdf), #31 / #32.

Rendering needs Poppler's pdftoppm; those tests skip where it is missing (the CI
unit job), and the Docker image job runs the real tools through scripts/mcp_smoke.py.
"""

import base64
import io
import shutil
import subprocess
import zipfile

import pytest
from fastmcp import Client, FastMCP
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

from src.domain.convert import pdf_images as P
from src.domain.convert.registry import DIRECT_ROUTES, plan_conversion
from src.domain.convert.types import ConversionFailedError, NoConversionPathError
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

HAS_PDFTOPPM = shutil.which("pdftoppm") is not None
needs_poppler = pytest.mark.skipif(not HAS_PDFTOPPM, reason="pdftoppm not installed")


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

def make_pdf(*sizes: tuple[float, float], encrypt: str | None = None,
             owner_only: bool = False) -> bytes:
    writer = PdfWriter()
    for w, h in sizes:
        writer.add_blank_page(w, h)
    if encrypt is not None:
        writer.encrypt(user_password="" if owner_only else encrypt,
                       owner_password=encrypt, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def image_bytes(fmt: str, size=(40, 20), color=(0, 0, 255), mode="RGB", **save) -> bytes:
    img = Image.new(mode, size, color)
    out = io.BytesIO()
    img.save(out, format=fmt, **save)
    return out.getvalue()


def marked_jpeg(orientation: int) -> bytes:
    """A 60x30 JPEG with a red top-left corner, tagged with an EXIF orientation"""
    img = Image.new("RGB", (60, 30), (255, 255, 255))
    img.paste((255, 0, 0), (0, 0, 15, 15))
    exif = img.getexif()
    exif[0x0112] = orientation
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95, exif=exif.tobytes(), dpi=(72, 72))
    return out.getvalue()


def make_zip(entries: list[tuple[str, bytes]]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, blob in entries:
            archive.writestr(name, blob)
    return out.getvalue()


def xobject(pdf: bytes, page: int = 0):
    page_obj = PdfReader(io.BytesIO(pdf)).pages[page]
    return page_obj["/Resources"]["/XObject"]["/Im0"].get_object()


def page_box(pdf: bytes, page: int = 0) -> tuple[float, float]:
    box = PdfReader(io.BytesIO(pdf)).pages[page].mediabox
    return float(box.width), float(box.height)


def render(pdf: bytes, tmp_path, dpi: int = 72) -> Image.Image:
    (tmp_path / "in.pdf").write_bytes(pdf)
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", "in.pdf", "out"],
                   cwd=tmp_path, check=True)
    return Image.open(tmp_path / "out.png").convert("RGB")


def is_red(px) -> bool:
    return px[0] > 200 and px[1] < 80 and px[2] < 80


# ----------------------------------------------------------------------------
# pdf -> images
# ----------------------------------------------------------------------------

@needs_poppler
async def test_renders_selected_pages_in_order_at_the_dpi():
    """TC-PDFIMG-001: one PNG per selected page, in the order asked, at the requested DPI."""
    pdf = make_pdf((72, 144), (144, 72), (72, 72))
    result = await P.render_pages(pdf, "png", 100, "3,1")
    assert [n for n, _ in result.images] == [3, 1]
    sizes = [Image.open(io.BytesIO(b)).size for _, b in result.images]
    assert sizes == [(100, 100), (100, 200)]
    assert result.total_pages == 3 and not result.lowered


@needs_poppler
@pytest.mark.parametrize("fmt,magic", [("jpg", b"\xff\xd8\xff"), ("jpeg", b"\xff\xd8\xff"),
                                       ("webp", b"RIFF"), ("png", b"\x89PNG")])
async def test_jpeg_webp_png_output(fmt, magic):
    """TC-PDFIMG-002"""
    result = await P.render_pages(make_pdf((72, 72)), fmt, 72, "")
    assert result.images[0][1].startswith(magic)


def test_page_dpi_is_lowered_only_over_the_pixel_cap():
    """TC-PDFIMG-003"""
    assert P.page_dpi(595, 842, 300) == 300                   # A4 at 300 = 8.7 MP
    lowered = P.page_dpi(3000, 3000, 300)
    assert lowered < 300
    assert (3000 / 72 * lowered) ** 2 <= P.MAX_PAGE_PIXELS
    assert (3000 / 72 * (lowered + 1)) ** 2 > P.MAX_PAGE_PIXELS


@needs_poppler
async def test_large_page_rendered_under_the_pixel_cap():
    """TC-PDFIMG-004: a poster-sized page comes out at a lower DPI, reported per page."""
    result = await P.render_pages(make_pdf((72, 72), (4000, 4000)), "png", 300, "")
    assert 1 not in result.lowered and 2 in result.lowered
    w, h = Image.open(io.BytesIO(result.images[1][1])).size
    assert w * h <= P.MAX_PAGE_PIXELS


@pytest.mark.parametrize("fmt,dpi,quality", [
    ("tiff", 150, 85), ("png", P.MIN_DPI - 1, 85), ("png", P.MAX_DPI + 1, 85),
    ("jpg", 150, 0), ("jpg", 150, 96),
])
async def test_bad_render_options_refused(fmt, dpi, quality):
    """TC-PDFIMG-005"""
    with pytest.raises(ConversionFailedError):
        await P.render_pages(make_pdf((72, 72)), fmt, dpi, "", quality)


async def test_page_cap(monkeypatch):
    """TC-PDFIMG-006: more pages than allowed is refused before anything is rendered."""
    monkeypatch.setattr(P, "MAX_RENDER_PAGES", 2)
    with pytest.raises(ConversionFailedError, match="at most 2 pages"):
        await P.render_pages(make_pdf((72, 72), (72, 72), (72, 72)), "png", 72, "")


async def test_password_protected_pdf_refused():
    """TC-PDFIMG-007"""
    with pytest.raises(ConversionFailedError, match="password-protected"):
        await P.render_pages(make_pdf((72, 72), encrypt="secret"), "png", 72, "")


@needs_poppler
async def test_owner_password_only_pdf_renders():
    """TC-PDFIMG-008: a PDF that opens without a password is not 'protected' for reading."""
    result = await P.render_pages(make_pdf((72, 72), encrypt="o", owner_only=True),
                                  "png", 72, "")
    assert len(result.images) == 1


@pytest.mark.parametrize("spec", ["5", "x", "2-1"])
async def test_bad_page_range(spec):
    """TC-PDFIMG-009"""
    with pytest.raises(ConversionFailedError) as info:
        await P.render_pages(make_pdf((72, 72), (72, 72)), "png", 72, spec)
    assert info.value.details["engine"] == "pdftoppm"


async def test_not_a_pdf():
    """TC-PDFIMG-010"""
    with pytest.raises(ConversionFailedError, match="not a readable PDF"):
        await P.render_pages(b"%PDF-1.4 garbage", "png", 72, "")


@needs_poppler
async def test_output_size_cap(monkeypatch):
    """TC-PDFIMG-011"""
    monkeypatch.setattr(P, "MAX_RENDER_OUTPUT_BYTES", 100)
    with pytest.raises(ConversionFailedError, match="larger than"):
        await P.render_pages(make_pdf((72, 72)), "png", 72, "")


async def test_missing_pdftoppm(monkeypatch):
    """TC-PDFIMG-012"""
    monkeypatch.setattr(P, "find_binary", lambda name: None)
    with pytest.raises(ConversionFailedError, match="not installed"):
        await P.render_pages(make_pdf((72, 72)), "png", 72, "")


def test_registry_points_pdf_to_image_at_the_tool():
    """TC-PDFIMG-013: convert_document pdf -> png names pdf_to_images instead of 'no path'."""
    with pytest.raises(NoConversionPathError, match="pdf_to_images"):
        plan_conversion("pdf", "png")


# ----------------------------------------------------------------------------
# images -> pdf
# ----------------------------------------------------------------------------

def test_png_alpha_flattened_onto_white_and_page_fits_image():
    """TC-PDFIMG-014: transparent pixels become white; 'fit' page = pixels at the file's DPI."""
    png = image_bytes("PNG", size=(144, 72), color=(255, 0, 0, 0), mode="RGBA", dpi=(144, 144))
    built = P.build_pdf_sync(png)
    assert page_box(built.data) == pytest.approx((72.0, 36.0), abs=0.05)
    image = xobject(built.data)
    assert image["/Filter"] == "/FlateDecode" and image["/ColorSpace"] == "/DeviceRGB"
    assert set(image.get_data()) == {255}


def test_fit_page_without_dpi_uses_96():
    """TC-PDFIMG-015"""
    built = P.build_pdf_sync(image_bytes("GIF", size=(96, 192)))
    assert page_box(built.data) == pytest.approx((72.0, 144.0), abs=0.05)


def test_jpeg_embedded_unchanged():
    """TC-PDFIMG-016"""
    jpg = image_bytes("JPEG", size=(50, 40), color=(10, 200, 30))
    built = P.build_pdf_sync(jpg)
    image = xobject(built.data)
    assert image["/Filter"] == "/DCTDecode"
    assert image._data == jpg
    assert built.passed_through == 1


def test_grayscale_jpeg_kept_gray():
    """TC-PDFIMG-017"""
    built = P.build_pdf_sync(image_bytes("JPEG", mode="L", color=128))
    assert xobject(built.data)["/ColorSpace"] == "/DeviceGray"


@needs_poppler
@pytest.mark.parametrize("orientation", range(1, 9))
def test_exif_orientation_applied_without_reencoding(orientation, tmp_path):
    """TC-PDFIMG-018: every EXIF orientation shows the picture upright, JPEG bytes unchanged."""
    jpg = marked_jpeg(orientation)
    built = P.build_pdf_sync(jpg)
    assert xobject(built.data)._data == jpg
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(jpg))).convert("RGB")
    got = render(built.data, tmp_path)
    assert got.size == expected.size
    w, h = got.size
    for x, y in [(3, 3), (w - 4, 3), (3, h - 4), (w - 4, h - 4)]:
        assert is_red(got.getpixel((x, y))) == is_red(expected.getpixel((x, y))), (x, y)


def test_exif_orientation_on_non_jpeg_applied_to_pixels():
    """TC-PDFIMG-019: a WebP tagged 'rotate 90' becomes a portrait page."""
    img = Image.new("RGB", (60, 30), (0, 0, 255))
    exif = img.getexif()
    exif[0x0112] = 6
    out = io.BytesIO()
    img.save(out, format="WEBP", exif=exif.tobytes(), lossless=True)
    built = P.build_pdf_sync(out.getvalue())
    image = xobject(built.data)
    assert (image["/Width"], image["/Height"]) == (30, 60)


def test_multipage_tiff_gives_a_page_each():
    """TC-PDFIMG-020"""
    frames = [Image.new("RGB", (20, 10), c) for c in [(255, 0, 0), (0, 255, 0), (0, 0, 255)]]
    out = io.BytesIO()
    frames[0].save(out, format="TIFF", save_all=True, append_images=frames[1:])
    built = P.build_pdf_sync(out.getvalue())
    assert built.pages == 3
    assert [bytes(xobject(built.data, i).get_data()[:3]) for i in range(3)] == [
        b"\xff\x00\x00", b"\x00\xff\x00", b"\x00\x00\xff"]


def test_animated_gif_first_frame_only():
    """TC-PDFIMG-021"""
    frames = [Image.new("RGB", (20, 10), c) for c in [(255, 0, 0), (0, 0, 255)]]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:])
    built = P.build_pdf_sync(out.getvalue())
    assert built.pages == 1
    assert bytes(xobject(built.data).get_data()[:3]) == b"\xff\x00\x00"


def test_zip_natural_order_and_skips():
    """TC-PDFIMG-022: pages follow natural name order; junk is skipped and listed."""
    archive = make_zip([
        ("scan10.png", image_bytes("PNG", size=(10, 10))),
        ("scan2.jpg", image_bytes("JPEG", size=(20, 10))),
        ("notes.txt", b"hello"),
        ("__MACOSX/._scan2.jpg", b"junk"),
        (".DS_Store", b"junk"),
        ("sub/", b""),
        ("Scan1.webp", image_bytes("WEBP", size=(30, 10))),
    ])
    built = P.build_pdf_sync(archive, src="zip")
    assert built.labels == ["Scan1.webp", "scan2.jpg", "scan10.png"]
    assert built.skipped == ["notes.txt"]
    assert [xobject(built.data, i)["/Width"] for i in range(3)] == [30, 20, 10]


def test_zip_without_images():
    """TC-PDFIMG-023"""
    with pytest.raises(ConversionFailedError, match="no images"):
        P.build_pdf_sync(make_zip([("a.txt", b"x")]), src="zip")


def test_zip_entry_count_cap(monkeypatch):
    """TC-PDFIMG-024"""
    monkeypatch.setattr(P, "MAX_ZIP_ENTRIES", 2)
    archive = make_zip([(f"{i}.txt", b"x") for i in range(3)])
    with pytest.raises(ConversionFailedError, match="entries"):
        P.build_pdf_sync(archive, src="zip")


def test_zip_unpacked_size_cap(monkeypatch):
    """TC-PDFIMG-025: a ZIP bomb is stopped by the unpacked total, not the archive size."""
    monkeypatch.setattr(P, "MAX_ZIP_UNCOMPRESSED", 1000)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.png", b"\0" * 600)
        archive.writestr("b.png", b"\0" * 600)
    with pytest.raises(ConversionFailedError, match="unpacks to more than"):
        P.build_pdf_sync(out.getvalue(), src="zip")


def test_zip_lying_file_size_refused_clearly(monkeypatch):
    """TC-PDFIMG-026: a header that understates the size cannot slip more data through;
    the entry is refused with a clear message."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.png", b"\0" * 5000)
    real_infolist = zipfile.ZipFile.infolist

    def lying(self):
        infos = real_infolist(self)
        for info in infos:
            info.file_size = 10
        return infos
    monkeypatch.setattr(zipfile.ZipFile, "infolist", lying)
    with pytest.raises(ConversionFailedError, match="cannot be read"):
        P.build_pdf_sync(out.getvalue(), src="zip")


def test_zip_encrypted_entry(monkeypatch):
    """TC-PDFIMG-027"""
    archive = make_zip([("a.png", image_bytes("PNG"))])
    real_infolist = zipfile.ZipFile.infolist

    def encrypted(self):
        infos = real_infolist(self)
        for info in infos:
            info.flag_bits |= 0x1
        return infos
    monkeypatch.setattr(zipfile.ZipFile, "infolist", encrypted)
    with pytest.raises(ConversionFailedError, match="password-protected"):
        P.build_pdf_sync(archive, src="zip")


def test_pixel_caps(monkeypatch):
    """TC-PDFIMG-028: per-image and total pixel limits (decompression-bomb guard)."""
    monkeypatch.setattr(P, "MAX_IMAGE_PIXELS", 399)
    with pytest.raises(ConversionFailedError, match="megapixels"):
        P.build_pdf_sync(image_bytes("PNG", size=(20, 20)))
    monkeypatch.setattr(P, "MAX_IMAGE_PIXELS", 400)
    monkeypatch.setattr(P, "MAX_TOTAL_PIXELS", 799)
    archive = make_zip([("a.png", image_bytes("PNG", size=(20, 20))),
                        ("b.png", image_bytes("PNG", size=(20, 20)))])
    with pytest.raises(ConversionFailedError, match="together"):
        P.build_pdf_sync(archive, src="zip")


def test_tiff_frame_pixel_cap(monkeypatch):
    """TC-PDFIMG-029: a later TIFF page larger than the first is still checked."""
    frames = [Image.new("RGB", (10, 10)), Image.new("RGB", (30, 30))]
    out = io.BytesIO()
    frames[0].save(out, format="TIFF", save_all=True, append_images=frames[1:])
    monkeypatch.setattr(P, "MAX_IMAGE_PIXELS", 500)
    with pytest.raises(ConversionFailedError, match="page 2"):
        P.build_pdf_sync(out.getvalue())


def test_page_count_cap(monkeypatch):
    """TC-PDFIMG-030"""
    monkeypatch.setattr(P, "MAX_PDF_PAGES", 2)
    archive = make_zip([(f"{i}.png", image_bytes("PNG")) for i in range(3)])
    with pytest.raises(ConversionFailedError, match="more than 2 images"):
        P.build_pdf_sync(archive, src="zip")
    frames = [Image.new("RGB", (5, 5)) for _ in range(3)]
    out = io.BytesIO()
    frames[0].save(out, format="TIFF", save_all=True, append_images=frames[1:])
    encoded = []
    real = P._encode_flate
    monkeypatch.setattr(P, "_encode_flate", lambda *a: encoded.append(1) or real(*a))
    with pytest.raises(ConversionFailedError, match="more than 2 images"):
        P.build_pdf_sync(out.getvalue())
    assert encoded == []                    # refused before any page was decoded


def test_output_cap(monkeypatch):
    """TC-PDFIMG-031"""
    monkeypatch.setattr(P, "MAX_PDF_OUTPUT_BYTES", 10)
    with pytest.raises(ConversionFailedError, match="larger than"):
        P.build_pdf_sync(image_bytes("JPEG"))


def test_a4_layout_auto_orientation_margins_and_centering():
    """TC-PDFIMG-032"""
    wide = image_bytes("PNG", size=(400, 100))
    built = P.build_pdf_sync(wide, page_size="a4")
    w, h = page_box(built.data)
    assert (round(w, 1), round(h, 1)) == (841.9, 595.3)                 # landscape
    content = PdfReader(io.BytesIO(built.data)).pages[0].get_contents().get_data().split()
    a, _, _, d, x, y = (float(v) for v in content[1:7])
    margin = 10 / 25.4 * 72
    assert a == pytest.approx(w - 2 * margin, abs=0.01)                 # fills the width
    assert x == pytest.approx(margin, abs=0.01)
    assert y == pytest.approx((h - d) / 2, abs=0.01)                    # centred

    portrait = P.build_pdf_sync(wide, page_size="letter", orientation="portrait",
                                margin_mm=0)
    assert page_box(portrait.data) == (612.0, 792.0)


def test_fit_with_margin():
    """TC-PDFIMG-033"""
    built = P.build_pdf_sync(image_bytes("PNG", size=(96, 96)), margin_mm=25.4)
    assert page_box(built.data) == pytest.approx((216.0, 216.0), abs=0.05)


@pytest.mark.parametrize("kwargs", [
    {"page_size": "a3"}, {"orientation": "sideways"}, {"margin_mm": -1},
    {"margin_mm": P.MAX_MARGIN_MM + 1},
])
def test_bad_layout_options(kwargs):
    """TC-PDFIMG-034"""
    with pytest.raises(ConversionFailedError):
        P.build_pdf_sync(image_bytes("PNG"), **kwargs)


def test_non_image_refused():
    """TC-PDFIMG-035"""
    with pytest.raises(ConversionFailedError, match="not an image"):
        P.build_pdf_sync(b"just some text")


def test_icc_profile_kept_only_when_it_matches():
    """TC-PDFIMG-036"""
    rgb = b"\0" * 16 + b"RGB " + b"\0" * 120
    gray = b"\0" * 16 + b"GRAY" + b"\0" * 120
    assert P._icc_for(rgb, 3) == rgb
    assert P._icc_for(rgb, 1) is None
    assert P._icc_for(gray, 1) == gray
    assert P._icc_for(b"short", 3) is None


def test_icc_profile_embedded():
    """TC-PDFIMG-037"""
    from PIL import ImageCms
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    built = P.build_pdf_sync(image_bytes("PNG", icc_profile=profile))
    space = xobject(built.data)["/ColorSpace"]
    assert space[0] == "/ICCBased" and space[1].get_object()["/N"] == 3


def test_mpo_first_image_passed_through():
    """TC-PDFIMG-038: a camera MPO keeps its main JPEG unchanged, without the extra images."""
    first = Image.new("RGB", (40, 30), (200, 0, 0))
    second = Image.new("RGB", (20, 15), (0, 0, 200))
    out = io.BytesIO()
    first.save(out, format="MPO", save_all=True, append_images=[second])
    mpo = out.getvalue()
    assert Image.open(io.BytesIO(mpo)).format == "MPO"
    built = P.build_pdf_sync(mpo)
    data = xobject(built.data)._data
    assert data.startswith(b"\xff\xd8") and len(data) < len(mpo)
    assert Image.open(io.BytesIO(data)).size == (40, 30)
    assert built.passed_through == 1


def test_cmyk_jpeg_reencoded_as_rgb():
    """TC-PDFIMG-039"""
    jpg = image_bytes("JPEG", mode="CMYK", color=(0, 255, 255, 0))
    built = P.build_pdf_sync(jpg)
    image = xobject(built.data)
    assert image["/ColorSpace"] == "/DeviceRGB" and image._data != jpg
    assert built.passed_through == 0


def test_written_pdf_is_well_formed():
    """TC-PDFIMG-040: pypdf reads it strictly (xref offsets, object count)."""
    archive = make_zip([("a.jpg", image_bytes("JPEG")), ("b.png", image_bytes("PNG"))])
    built = P.build_pdf_sync(archive, src="zip")
    reader = PdfReader(io.BytesIO(built.data), strict=True)
    assert len(reader.pages) == 2


def test_registry_image_to_pdf_is_the_picture_not_ocr():
    """TC-PDFIMG-041: png -> pdf used to OCR the picture into text; now one direct hop."""
    for src in ("png", "jpg", "webp", "gif", "bmp", "tiff"):
        assert DIRECT_ROUTES[(src, "pdf")].engine == "images-to-pdf"
    plan = plan_conversion("png", "pdf")
    assert [r.engine for r in plan] == ["images-to-pdf"]


# ----------------------------------------------------------------------------
# Through a real MCP client
# ----------------------------------------------------------------------------

def _client():
    mcp = FastMCP("pdf-images-test")
    register_morphyard_tools(mcp)
    return Client(mcp)


@needs_poppler
async def test_tool_one_page_returns_the_image():
    """TC-PDFIMG-042"""
    async with _client() as client:
        result = await client.call_tool("pdf_to_images", {
            "pages": "2", "dpi": 72,
            "file_content": base64.b64encode(make_pdf((72, 72), (144, 72))).decode(),
            "file_name": "report.pdf",
        })
    resource = result.content[1].resource
    assert resource.mime_type == "image/png"
    assert str(resource.uri).endswith("report_p2.png")
    assert Image.open(io.BytesIO(base64.b64decode(resource.blob))).size == (144, 72)


@needs_poppler
async def test_tool_several_pages_return_one_zip():
    """TC-PDFIMG-043"""
    async with _client() as client:
        result = await client.call_tool("pdf_to_images", {
            "image_format": "jpg", "dpi": 72,
            "file_content": base64.b64encode(make_pdf(*[(72, 72)] * 3)).decode(),
            "file_name": "report.pdf",
        })
    assert len(result.content) == 2
    resource = result.content[1].resource
    assert resource.mime_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(resource.blob))) as archive:
        assert archive.namelist() == ["report_p01.jpg", "report_p02.jpg", "report_p03.jpg"]
    assert "3 of 3 pages" in result.content[0].text


async def test_tool_refuses_non_pdf_and_bad_options_before_queueing():
    """TC-PDFIMG-044"""
    async with _client() as client:
        result = await client.call_tool("pdf_to_images", {
            "file_content": base64.b64encode(image_bytes("PNG")).decode(),
            "file_name": "a.png",
        })
        assert "not a PDF" in result.content[0].text
        with pytest.raises(Exception, match="dpi must be"):
            await client.call_tool("pdf_to_images", {
                "dpi": 9999,
                "file_content": base64.b64encode(make_pdf((72, 72))).decode(),
                "file_name": "a.pdf",
            })


async def test_tool_images_to_pdf_from_zip():
    """TC-PDFIMG-045"""
    archive = make_zip([("b.png", image_bytes("PNG")), ("a.jpg", image_bytes("JPEG")),
                        ("readme.md", b"# hi")])
    async with _client() as client:
        result = await client.call_tool("images_to_pdf", {
            "page_size": "a4",
            "file_content": base64.b64encode(archive).decode(),
            "file_name": "holiday photos.zip",
        })
    text = result.content[0].text
    assert "2-page PDF" in text and "a.jpg, b.png" in text and "readme.md" in text
    resource = result.content[1].resource
    assert resource.mime_type == "application/pdf"
    assert str(resource.uri).endswith("holiday%20photos.pdf") or \
        str(resource.uri).endswith("holiday photos.pdf")
    assert len(PdfReader(io.BytesIO(base64.b64decode(resource.blob))).pages) == 2


async def test_tool_images_to_pdf_given_a_pdf():
    """TC-PDFIMG-046"""
    async with _client() as client:
        result = await client.call_tool("images_to_pdf", {
            "file_content": base64.b64encode(make_pdf((72, 72))).decode(),
        })
    assert "already a PDF" in result.content[0].text


async def test_tool_images_to_pdf_size_limit(monkeypatch):
    """TC-PDFIMG-047"""
    from src.fastmcp_tools import pdf_image_tools
    monkeypatch.setattr(pdf_image_tools, "MAX_INPUT_MB", 0)
    async with _client() as client:
        with pytest.raises(Exception, match="too large"):
            await client.call_tool("images_to_pdf", {
                "file_content": base64.b64encode(image_bytes("PNG")).decode(),
            })


async def test_convert_document_png_to_pdf_keeps_the_picture():
    """TC-PDFIMG-048"""
    async with _client() as client:
        result = await client.call_tool("convert_document", {
            "target_format": "pdf", "output_name": "photo",
            "file_content": base64.b64encode(image_bytes("JPEG")).decode(),
            "file_name": "photo.jpg",
        })
    pdf = base64.b64decode(result.content[1].resource.blob)
    assert xobject(pdf)["/Filter"] == "/DCTDecode"


async def test_tools_check_options_before_taking_a_slot(monkeypatch):
    """TC-PDFIMG-049: a bad option is answered at once, even when the server is busy."""
    from contextlib import asynccontextmanager

    from src.fastmcp_tools import pdf_image_tools

    @asynccontextmanager
    async def busy():
        raise RuntimeError("slot taken")
        yield
    monkeypatch.setattr(pdf_image_tools, "conversion_slot", busy)
    async with _client() as client:
        with pytest.raises(Exception, match="dpi must be"):
            await client.call_tool("pdf_to_images", {
                "dpi": 9999,
                "file_content": base64.b64encode(make_pdf((72, 72))).decode(),
                "file_name": "a.pdf",
            })
        with pytest.raises(Exception, match="page_size must be"):
            await client.call_tool("images_to_pdf", {
                "page_size": "a3",
                "file_content": base64.b64encode(image_bytes("PNG")).decode(),
            })
