"""PDF page tools: merge, delete, reorder, watermark, page numbers (issues #24, #25, #26, #28)."""

import asyncio
import base64
import io
import shutil
import subprocess

import pytest
from fastmcp import Client, FastMCP
from PIL import Image
from pypdf import PdfReader, PdfWriter

from src.domain.convert import limiter as limiter_mod
from src.domain.convert import pdf_pages as pp
from src.domain.convert.pdf_pages import InvalidPdfOptionError
from src.domain.convert.types import ConversionFailedError
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

NEW_TOOLS = ["pdf_merge", "pdf_delete_pages", "pdf_reorder_pages", "pdf_watermark",
             "pdf_page_numbers"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pdf(n=5, *, sizes=None, rotations=None, outline=False, encrypt=False,
             prefix="A") -> bytes:
    """Page i (1-based) is 100+i points wide, so pages can be told apart by width."""
    w = PdfWriter()
    for i in range(n):
        width, height = sizes[i] if sizes else (100 + i + 1, 400)
        page = w.add_blank_page(width, height)
        if rotations and rotations[i]:
            page.rotate(rotations[i])
    if outline:
        top = w.add_outline_item(f"{prefix} one", 0)
        w.add_outline_item(f"{prefix} one.sub", 1, parent=top)
        w.add_outline_item(f"{prefix} last", n - 1)
    if encrypt:
        w.encrypt(user_password="", owner_password="owner", algorithm="AES-256")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def widths(data: bytes) -> list[int]:
    return [round(float(p.mediabox.width)) - 100 for p in PdfReader(io.BytesIO(data)).pages]


def outline(data: bytes) -> list:
    reader = PdfReader(io.BytesIO(data))

    def walk(items, depth=0):
        out = []
        for item in items:
            if isinstance(item, list):
                out.extend(walk(item, depth + 1))
            else:
                out.append((depth, item.title, reader.get_destination_page_number(item) + 1))
        return out
    return walk(reader.outline)


def texts(data: bytes) -> list[str]:
    return [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]


def png(size=(40, 20), mode="RGBA", fmt="PNG") -> bytes:
    color = {"RGBA": (200, 0, 0, 128), "L": 90}.get(mode, (200, 0, 0))
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _restore_limiter():
    saved = limiter_mod._limiter
    yield
    limiter_mod._limiter = saved


# ---------------------------------------------------------------------------
# Merge (#24)
# ---------------------------------------------------------------------------

def test_merge_keeps_order_and_adds_one_bookmark_per_file():
    """TC-PDFPG-001"""
    a = make_pdf(3, outline=True, prefix="A")
    b = make_pdf(2, sizes=[(301, 400), (302, 400)], outline=True, prefix="B")
    data, pages = run(pp.merge_pdfs([("Alpha", a), ("Beta", b)]))
    assert pages == 5
    assert widths(data) == [1, 2, 3, 201, 202]
    assert outline(data) == [
        (0, "Alpha", 1), (1, "A one", 1), (2, "A one.sub", 2), (1, "A last", 3),
        (0, "Beta", 4), (1, "B one", 4), (2, "B one.sub", 5), (1, "B last", 5),
    ]


def test_merge_without_bookmarks_adds_none():
    """TC-PDFPG-002"""
    data, _ = run(pp.merge_pdfs([("A", make_pdf(2, outline=True)), ("B", make_pdf(1))],
                                bookmarks=False))
    assert outline(data) == []


@pytest.mark.parametrize("inputs, needle", [
    ([("A", b"x")], "at least 2"),
    ([("A", b"x")] * (pp.MAX_MERGE_FILES + 1), f"at most {pp.MAX_MERGE_FILES} files"),
    ([("A", b"x" * (26 * 1024 * 1024))] * 2, "MB"),
])
def test_merge_refuses_too_few_too_many_or_too_large(inputs, needle):
    """TC-PDFPG-003"""
    with pytest.raises(InvalidPdfOptionError, match=needle):
        pp.validate_merge_inputs(inputs)


def test_merge_refuses_an_encrypted_input_naming_it():
    """TC-PDFPG-004"""
    with pytest.raises(ConversionFailedError, match=r"file #2 \('Locked'\) is encrypted"):
        run(pp.merge_pdfs([("Open", make_pdf(1)), ("Locked", make_pdf(1, encrypt=True))]))


def test_merge_refuses_a_result_over_the_page_limit(monkeypatch):
    """TC-PDFPG-005"""
    monkeypatch.setattr(pp, "MAX_PAGES", 6)
    with pytest.raises(ConversionFailedError, match="would have 8 pages"):
        run(pp.merge_pdfs([("A", make_pdf(4)), ("B", make_pdf(4))]))


def test_a_single_input_over_the_page_limit_is_refused(monkeypatch):
    """TC-PDFPG-006"""
    monkeypatch.setattr(pp, "MAX_PAGES", 3)
    with pytest.raises(ConversionFailedError, match="has 4 pages; the limit is 3"):
        run(pp.reorder_pages(make_pdf(4), "reverse"))


def test_a_pdf_without_pages_is_refused():
    """TC-PDFPG-008"""
    buf = io.BytesIO()
    PdfWriter().write(buf)
    with pytest.raises(ConversionFailedError, match="has no pages"):
        run(pp.watermark_pdf(buf.getvalue(), pp.WatermarkOptions(text="X")))


def test_merge_refuses_a_damaged_input():
    """TC-PDFPG-007"""
    with pytest.raises(ConversionFailedError, match="file #1"):
        run(pp.merge_pdfs([("Bad", b"%PDF-1.7 garbage"), ("B", make_pdf(1))]))


# ---------------------------------------------------------------------------
# Delete and reorder (#26)
# ---------------------------------------------------------------------------

def test_delete_removes_the_pages_and_keeps_the_rest_in_order():
    """TC-PDFPG-010"""
    data, deleted, left = run(pp.delete_pages(make_pdf(6), "2,4-5"))
    assert widths(data) == [1, 3, 6]
    assert deleted == [2, 4, 5] and left == 3


def test_delete_keeps_bookmarks_of_surviving_pages():
    """TC-PDFPG-011"""
    data, _, _ = run(pp.delete_pages(make_pdf(5, outline=True), "2"))
    assert outline(data) == [(0, "A one", 1), (0, "A last", 4)]


@pytest.mark.parametrize("spec, error, needle", [
    ("1-5", InvalidPdfOptionError, "all 5 pages"),
    ("", InvalidPdfOptionError, "empty"),
    ("  ", InvalidPdfOptionError, "empty"),
    ("6", ConversionFailedError, "out of bounds"),
    ("0", ConversionFailedError, "out of bounds"),
    ("two", ConversionFailedError, "invalid page range"),
])
def test_delete_refuses_bad_selections(spec, error, needle):
    """TC-PDFPG-012"""
    with pytest.raises(error, match=needle):
        run(pp.delete_pages(make_pdf(5), spec))


@pytest.mark.parametrize("spec, expected", [
    ("3,1,2,4-5", [3, 1, 2, 4, 5]),
    ("reverse", [5, 4, 3, 2, 1]),
    (" Reverse ", [5, 4, 3, 2, 1]),
    ("5-1", [5, 4, 3, 2, 1]),
    ("2, 1, 3-", [2, 1, 3, 4, 5]),
    ("-2,5,4,3", [1, 2, 5, 4, 3]),
    ("1,2,3,4,5", [1, 2, 3, 4, 5]),
])
def test_reorder_follows_the_given_order(spec, expected):
    """TC-PDFPG-013"""
    data, used = run(pp.reorder_pages(make_pdf(5), spec))
    assert widths(data) == expected
    assert used == expected


@pytest.mark.parametrize("spec, needle", [
    ("3,1,2", r"leaves out page\(s\) 4-5 of 5"),
    ("1,2,3,4,5,2", r"page\(s\) 2 more than once"),
    ("1-5,1-2", r"page\(s\) 1-2 more than once"),
    ("1,2,3,4,6", "page 6 in '6' does not exist"),
    ("0,1,2,3,4,5", "page 0"),
    ("1,2,x,4,5", "invalid entry 'x'"),
    ("1;2", "invalid entry"),
    ("-", "invalid entry"),
    ("", "order is empty"),
])
def test_reorder_refuses_orders_that_lose_or_repeat_pages(spec, needle):
    """TC-PDFPG-014"""
    with pytest.raises(InvalidPdfOptionError, match=needle):
        pp.parse_page_order(spec, 5)


def test_reorder_moves_bookmarks_with_their_pages():
    """TC-PDFPG-015"""
    data, _ = run(pp.reorder_pages(make_pdf(5, outline=True), "reverse"))
    assert outline(data) == [(0, "A one", 5), (1, "A one.sub", 4), (0, "A last", 1)]


def test_reorder_and_delete_keep_page_rotation():
    """TC-PDFPG-016"""
    data, _ = run(pp.reorder_pages(make_pdf(3, rotations=[0, 90, 0]), "2,1,3"))
    assert [p.rotation for p in PdfReader(io.BytesIO(data)).pages] == [90, 0, 0]


# ---------------------------------------------------------------------------
# Geometry shared by watermark and page numbers
# ---------------------------------------------------------------------------

def _apply(ctm, u, v):
    a, b, c, d, e, f = ctm
    return (a * u + c * v + e, b * u + d * v + f)


@pytest.mark.parametrize("rotation, displayed, bottom_left, top_right", [
    # crop box (10, 20)-(210, 320): 200 wide, 300 high
    (0, (200, 300), (10, 20), (210, 320)),
    (90, (300, 200), (210, 20), (10, 320)),
    (180, (200, 300), (210, 320), (10, 20)),
    (270, (300, 200), (10, 320), (210, 20)),
])
def test_display_box_maps_the_visible_corners_to_the_crop_box(
        rotation, displayed, bottom_left, top_right):
    """TC-PDFPG-020: what a reader sees as bottom-left / top-right of a turned page."""
    w = PdfWriter()
    page = w.add_blank_page(400, 400)
    page.cropbox.lower_left = (10, 20)
    page.cropbox.upper_right = (210, 320)
    if rotation:
        page.rotate(rotation)
    width, height, ctm = pp.display_box(page)
    assert (width, height) == displayed
    assert _apply(ctm, 0, 0) == bottom_left
    assert _apply(ctm, width, height) == top_right


def _ink_box(pdf: bytes, page: int):
    """Bounding box of non-white pixels, as fractions (left, top, right, bottom)."""
    out = subprocess.run(
        ["pdftoppm", "-f", str(page), "-l", str(page), "-r", "36", "-gray", "-png", "-", ],
        input=pdf, capture_output=True, check=True,
    ).stdout
    img = Image.open(io.BytesIO(out)).point(lambda p: 255 if p < 200 else 0)
    left, top, right, bottom = img.getbbox()
    return left / img.width, top / img.height, right / img.width, bottom / img.height


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="needs poppler's pdftoppm")
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_page_number_sits_where_the_reader_expects_on_a_turned_page(rotation):
    """TC-PDFPG-021: rendered, the number is in the bottom-right corner as displayed."""
    src = make_pdf(1, sizes=[(300, 500)], rotations=[rotation])
    data, _ = run(pp.add_page_numbers(src, pp.PageNumberOptions(
        number_format="88", number_position="bottom-right", font_size=24, margin=20)))
    left, top, right, bottom = _ink_box(data, 1)
    assert left > 0.6 and top > 0.8, (rotation, left, top, right, bottom)


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="needs poppler's pdftoppm")
def test_a_header_sits_at_the_top_left_of_a_landscape_rotated_page():
    """TC-PDFPG-022"""
    src = make_pdf(1, sizes=[(500, 300)], rotations=[90])
    data, _ = run(pp.add_page_numbers(src, pp.PageNumberOptions(
        number_format="", header_text="TITLE", header_align="left", font_size=24,
        margin=20)))
    left, top, right, bottom = _ink_box(data, 1)
    assert right < 0.5 and bottom < 0.2, (left, top, right, bottom)


# ---------------------------------------------------------------------------
# Watermark (#25)
# ---------------------------------------------------------------------------

def test_text_watermark_only_on_the_chosen_pages():
    """TC-PDFPG-030"""
    data, stamped = run(pp.watermark_pdf(make_pdf(4), pp.WatermarkOptions(
        text="CONFIDENTIAL", pages="2-3")))
    assert stamped == 2
    assert ["CONFIDENTIAL" in t for t in texts(data)] == [False, True, True, False]


def test_chinese_watermark_text_is_drawn():
    """TC-PDFPG-031"""
    data, _ = run(pp.watermark_pdf(make_pdf(1), pp.WatermarkOptions(text="機密文件 草稿")))
    assert "機密文件" in texts(data)[0].replace("\n", "")


@pytest.mark.parametrize("position", ["center", "diagonal", "tiled"])
def test_every_position_works_on_mixed_sizes_and_rotations(position):
    """TC-PDFPG-032"""
    src = make_pdf(4, sizes=[(595, 842), (842, 595), (200, 200), (612, 1008)],
                   rotations=[0, 90, 180, 270])
    data, stamped = run(pp.watermark_pdf(src, pp.WatermarkOptions(
        text="DRAFT", position=position, font_size=30)))
    assert stamped == 4
    assert all("DRAFT" in t for t in texts(data))
    reader = PdfReader(io.BytesIO(data))
    assert [p.rotation for p in reader.pages] == [0, 90, 180, 270]
    assert [round(float(p.mediabox.width)) for p in reader.pages] == [595, 842, 200, 612]


def test_tiled_watermark_repeats_but_is_capped():
    """TC-PDFPG-033"""
    few = pp._tile_centres(595, 842, 100, 30)
    assert len(few) > 4
    many = pp._tile_centres(14400, 14400, 6, 6)
    assert len(many) <= pp.MAX_TILES_PER_PAGE * 1.3


def test_a_long_single_watermark_is_shrunk_to_fit_the_page():
    """TC-PDFPG-034"""
    drawn = []

    class Canvas:
        def __getattr__(self, name):
            return lambda *a, **k: None

        def setFont(self, font, size):
            drawn.append(size)

    draw = pp._watermark_drawer(pp.WatermarkOptions(text="X" * 150, position="center",
                                                    rotation=0, font_size=100))
    draw(Canvas(), 300, 300, 0, 1)
    assert drawn and max(drawn) < 100
    width = pp._width(pp._runs("X" * 150), max(drawn))
    assert width <= 0.9 * 300 + 1


@pytest.mark.parametrize("image", [png(), png(mode="RGB", fmt="JPEG"), png(mode="L")])
def test_image_watermark_png_or_jpeg(image):
    """TC-PDFPG-035"""
    data, stamped = run(pp.watermark_pdf(make_pdf(2), pp.WatermarkOptions(
        image=pp.load_watermark_image(image), opacity=0.5)))
    assert stamped == 2
    for page in PdfReader(io.BytesIO(data)).pages:
        assert len(page.images) == 1


def test_a_transparent_png_keeps_its_transparency():
    """TC-PDFPG-036"""
    data, _ = run(pp.watermark_pdf(make_pdf(1), pp.WatermarkOptions(
        image=pp.load_watermark_image(png()))))
    xobjects = PdfReader(io.BytesIO(data)).pages[0]["/Resources"]["/XObject"]
    found = []

    def visit(objs):
        for ref in objs.values():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image":
                found.append(obj)
            elif obj.get("/Resources", {}).get("/XObject"):
                visit(obj["/Resources"]["/XObject"])
    visit(xobjects)
    assert found and "/SMask" in found[0]


@pytest.mark.parametrize("blob, needle", [
    (png(fmt="GIF", mode="RGB"), "must be PNG or JPEG"),
    (b"not an image", "not a PNG or JPEG"),
])
def test_watermark_image_must_be_png_or_jpeg(blob, needle):
    """TC-PDFPG-037"""
    with pytest.raises(InvalidPdfOptionError, match=needle):
        pp.load_watermark_image(blob)


def test_watermark_image_size_limits(monkeypatch):
    """TC-PDFPG-038"""
    monkeypatch.setattr(pp, "MAX_WATERMARK_IMAGE_PIXELS", 100)
    with pytest.raises(InvalidPdfOptionError, match="pixels"):
        pp.load_watermark_image(png(size=(20, 20)))
    monkeypatch.setattr(pp, "MAX_WATERMARK_IMAGE_MB", 0)
    with pytest.raises(InvalidPdfOptionError, match="MB"):
        pp.load_watermark_image(png())


@pytest.mark.parametrize("kwargs, needle", [
    ({}, "exactly one of text or watermark_image"),
    ({"text": "   "}, "exactly one"),
    ({"text": "A", "image": "IMG"}, "exactly one"),
    ({"text": "A" * 201}, "201 characters"),
    ({"text": "A", "position": "corner"}, "position must be one of"),
    ({"text": "A", "opacity": 0}, "opacity"),
    ({"text": "A", "opacity": 1.5}, "opacity"),
    ({"text": "A", "font_size": 2}, "font_size"),
    ({"text": "A", "font_size": 301}, "font_size"),
    ({"text": "A", "rotation": 400}, "rotation"),
    ({"text": "A", "image_scale": 0}, "image_scale"),
    ({"text": "A", "color": "rgb(1,2,3)"}, "unknown colour"),
])
def test_watermark_options_are_checked(kwargs, needle):
    """TC-PDFPG-039"""
    if kwargs.get("image") == "IMG":
        kwargs = {**kwargs, "image": pp.load_watermark_image(png())}
    with pytest.raises(InvalidPdfOptionError, match=needle):
        pp.validate_watermark(pp.WatermarkOptions(**kwargs))


def test_watermark_keeps_bookmarks():
    """TC-PDFPG-040"""
    data, _ = run(pp.watermark_pdf(make_pdf(3, outline=True), pp.WatermarkOptions(text="X")))
    assert outline(data) == outline(make_pdf(3, outline=True))


@pytest.mark.parametrize("value, rgb", [
    ("#808080", (128, 128, 128)), ("#f00", (255, 0, 0)), ("00FF00", (0, 255, 0)),
    ("navy", (0, 0, 128)), (" Red ", (255, 0, 0)),
])
def test_colours_are_parsed_by_hand(value, rgb):
    """TC-PDFPG-041"""
    c = pp.parse_color(value)
    assert tuple(round(x * 255) for x in (c.red, c.green, c.blue)) == rgb


@pytest.mark.parametrize("value", ["__import__('os')", "red+blue", "", "#12345",
                                   "Color(1,0,0)", "0xff0000"])
def test_anything_else_is_not_a_colour(value):
    """TC-PDFPG-042: never handed to reportlab's own parser."""
    with pytest.raises(InvalidPdfOptionError, match="unknown colour"):
        pp.parse_color(value)


# ---------------------------------------------------------------------------
# Page numbers, header and footer (#28)
# ---------------------------------------------------------------------------

def test_page_numbers_with_total():
    """TC-PDFPG-050"""
    data, stamped = run(pp.add_page_numbers(make_pdf(3), pp.PageNumberOptions(
        number_format="{page} / {total}")))
    assert stamped == 3
    assert [t.strip() for t in texts(data)] == ["1 / 3", "2 / 3", "3 / 3"]


def test_skip_first_page_and_start_number():
    """TC-PDFPG-051"""
    data, stamped = run(pp.add_page_numbers(make_pdf(4), pp.PageNumberOptions(
        number_format="P{page}/{total}", start_number=5, skip_first_page=True)))
    assert stamped == 3
    assert [t.strip() for t in texts(data)] == ["", "P5/7", "P6/7", "P7/7"]


def test_only_chosen_pages_are_stamped_and_counted():
    """TC-PDFPG-052"""
    data, _ = run(pp.add_page_numbers(make_pdf(5), pp.PageNumberOptions(
        number_format="{page}-{total}", pages="2,4-5")))
    assert [t.strip() for t in texts(data)] == ["", "1-3", "", "2-3", "3-3"]


def test_chinese_header_and_footer():
    """TC-PDFPG-053"""
    data, _ = run(pp.add_page_numbers(make_pdf(2, sizes=[(595, 842)] * 2),
                                      pp.PageNumberOptions(
        number_format="第 {page} 頁", header_text="年度報告 Annual Report",
        footer_text="機密 {page}/{total}", footer_align="left")))
    first = " ".join(texts(data)[0].split())
    assert "年度報告" in first and "Annual Report" in first
    assert "機密 1/2" in first and "第 1 頁" in first


def test_skipping_the_only_chosen_page_is_refused():
    """TC-PDFPG-054"""
    with pytest.raises(InvalidPdfOptionError, match="no page is left"):
        run(pp.add_page_numbers(make_pdf(3), pp.PageNumberOptions(
            pages="1", skip_first_page=True)))


@pytest.mark.parametrize("kwargs, needle", [
    ({"number_format": "{page} of {pages}"}, r"\{pages\}"),
    ({"number_format": "{page.__class__}"}, "only placeholders"),
    ({"header_text": "{0}"}, "only placeholders"),
    ({"number_format": "", "header_text": " ", "footer_text": ""}, "nothing to add"),
    ({"number_position": "middle"}, "number_position"),
    ({"header_text": "T", "header_align": "justify"}, "header_align"),
    ({"footer_text": "F", "footer_align": "center"}, "overlap at bottom-center"),
    ({"header_text": "H", "header_align": "right", "number_position": "top-right"},
     "overlap at top-right"),
    ({"font_size": 5}, "font_size"),
    ({"font_size": 73}, "font_size"),
    ({"margin": -1}, "margin"),
    ({"margin": 201}, "margin"),
    ({"color": "bogus"}, "unknown colour"),
    ({"footer_text": "x" * 201}, "201 characters"),
])
def test_page_number_options_are_checked(kwargs, needle):
    """TC-PDFPG-055"""
    with pytest.raises(InvalidPdfOptionError, match=needle):
        pp.validate_page_numbers(pp.PageNumberOptions(**kwargs))


def test_placeholders_are_replaced_without_str_format():
    """TC-PDFPG-056"""
    assert pp._fill("{page}{{x}} {total}", 3, 9) == "3{{x}} 9"


def test_footer_with_page_and_no_number_format_is_fine():
    """TC-PDFPG-057"""
    data, _ = run(pp.add_page_numbers(make_pdf(2), pp.PageNumberOptions(
        number_format="", footer_text="Page {page}", footer_align="center")))
    assert [t.strip() for t in texts(data)] == ["Page 1", "Page 2"]


def test_a_long_header_is_shrunk_to_the_page_width():
    """TC-PDFPG-058"""
    drawn = []

    class Canvas:
        def __getattr__(self, name):
            return lambda *a, **k: None

        def setFont(self, font, size):
            drawn.append(size)

        def drawString(self, x, y, s):
            drawn.append(("x", x))

    draw = pp._page_number_drawer(pp.PageNumberOptions(
        number_format="", header_text="W" * 120, font_size=20, margin=20))
    draw(Canvas(), 300, 400, 0, 1)
    assert max(s for s in drawn if not isinstance(s, tuple)) < 20
    assert min(x for tag, x in [s for s in drawn if isinstance(s, tuple)]) >= 20 - 0.01


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda d: pp.delete_pages(d, "1"),
    lambda d: pp.reorder_pages(d, "reverse"),
    lambda d: pp.watermark_pdf(d, pp.WatermarkOptions(text="X")),
    lambda d: pp.add_page_numbers(d, pp.PageNumberOptions()),
])
def test_encrypted_input_is_refused_by_every_tool(call):
    """TC-PDFPG-060"""
    with pytest.raises(ConversionFailedError, match="encrypted.*Remove the password"):
        run(call(make_pdf(2, encrypt=True)))


def test_output_over_the_size_limit_is_refused(monkeypatch):
    """TC-PDFPG-061"""
    monkeypatch.setattr(pp, "MAX_OUTPUT_MB", 0)
    with pytest.raises(ConversionFailedError, match="limit is 0 MB"):
        run(pp.reorder_pages(make_pdf(2), "reverse"))


def test_without_a_truetype_cjk_font_chinese_falls_back_to_a_cid_font(monkeypatch):
    """TC-PDFPG-062"""
    monkeypatch.setattr(pp, "_CJK_FONTS", ())
    pp._fonts.cache_clear()
    try:
        runs = pp._runs("機密 OK")
        assert runs[0] == (pp._CID_FALLBACK, "機密 ")
        data, _ = run(pp.watermark_pdf(make_pdf(1), pp.WatermarkOptions(text="機密")))
        assert "機密" in texts(data)[0]
    finally:
        pp._fonts.cache_clear()


def test_latin_and_cjk_runs_switch_fonts():
    """TC-PDFPG-063"""
    names = [font for font, _ in pp._runs("Page 1 第一頁")]
    assert len(set(names)) == 2 or names == [pp._CID_FALLBACK]


# ---------------------------------------------------------------------------
# The MCP tools
# ---------------------------------------------------------------------------

def _client():
    mcp = FastMCP("pdf-pages-test")
    register_morphyard_tools(mcp)
    return Client(mcp)


def _pdf_blocks(result):
    return [base64.b64decode(block.resource.blob) for block in result.content
            if getattr(block, "resource", None) is not None]


async def test_the_tools_are_registered_without_an_output_schema():
    """TC-PDFPG-070: list-returning tools must not declare one (see morphyard_tools)."""
    async with _client() as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in NEW_TOOLS:
        assert name in tools
        assert tools[name].outputSchema is None


@pytest.mark.parametrize("tool, args, check", [
    ("pdf_delete_pages", {"pages": "1"}, lambda d: widths(d) == [2, 3]),
    ("pdf_reorder_pages", {"order": "3,1,2"}, lambda d: widths(d) == [3, 1, 2]),
    ("pdf_watermark", {"text": "SECRET"}, lambda d: "SECRET" in texts(d)[0]),
    ("pdf_watermark", {"watermark_image": b64(png())},
     lambda d: len(PdfReader(io.BytesIO(d)).pages[2].images) == 1),
    ("pdf_page_numbers", {"number_format": "{page}/{total}"},
     lambda d: texts(d)[2].strip() == "3/3"),
])
async def test_each_tool_returns_a_pdf(tool, args, check):
    """TC-PDFPG-071"""
    async with _client() as client:
        result = await client.call_tool(tool, {**args, "file_content": b64(make_pdf(3)),
                                               "file_name": "doc.pdf"})
    blobs = _pdf_blocks(result)
    assert len(blobs) == 1 and check(blobs[0])
    assert "doc_" in result.content[1].resource.uri


async def test_merge_tool_takes_a_list_of_files():
    """TC-PDFPG-072"""
    files = [{"file_content": b64(make_pdf(2)), "file_name": "First.pdf"},
             {"file_content": b64(make_pdf(1, sizes=[(301, 400)])), "file_name": "Second.pdf"}]
    async with _client() as client:
        result = await client.call_tool("pdf_merge", {"files": files})
    data = _pdf_blocks(result)[0]
    assert widths(data) == [1, 2, 201]
    assert [title for _, title, _ in outline(data)] == ["First", "Second"]
    assert "First_merged" in result.content[1].resource.uri


async def test_merge_tool_names_a_file_that_is_not_a_pdf():
    """TC-PDFPG-073"""
    files = [{"file_content": b64(make_pdf(1)), "file_name": "a.pdf"},
             {"file_content": b64(png()), "file_name": "b.png"}]
    async with _client() as client:
        result = await client.call_tool("pdf_merge", {"files": files})
    assert "File #2 (b.png) is not a PDF (detected png)" in result.content[0].text
    assert not _pdf_blocks(result)


async def test_merge_tool_refuses_a_single_file():
    """TC-PDFPG-074"""
    async with _client() as client:
        result = await client.call_tool("pdf_merge", {"files": [
            {"file_content": b64(make_pdf(1)), "file_name": "a.pdf"}]}, raise_on_error=False)
    assert result.is_error


@pytest.mark.parametrize("tool, args", [
    ("pdf_delete_pages", {"pages": "1"}),
    ("pdf_reorder_pages", {"order": "1"}),
    ("pdf_watermark", {"text": "X"}),
    ("pdf_page_numbers", {}),
])
async def test_a_non_pdf_is_answered_with_a_hint(tool, args):
    """TC-PDFPG-075"""
    async with _client() as client:
        result = await client.call_tool(tool, {**args, "file_content": b64(png()),
                                               "file_name": "x.png"})
    assert "not a PDF (detected png)" in result.content[0].text


async def test_bad_options_are_answered_before_queueing_when_busy():
    """TC-PDFPG-076: validation comes before the conversion slot."""
    lim = limiter_mod.configure(1, 0, 30)
    await lim.acquire()
    try:
        async with _client() as client:
            r1 = await client.call_tool("pdf_watermark", {
                "text": "X", "opacity": 3, "file_content": b64(make_pdf(1)),
                "file_name": "a.pdf"}, raise_on_error=False)
            r2 = await client.call_tool("pdf_page_numbers", {
                "number_format": "{nope}", "file_content": b64(make_pdf(1)),
                "file_name": "a.pdf"}, raise_on_error=False)
            r3 = await client.call_tool("pdf_watermark", {
                "text": "X", "file_content": b64(make_pdf(1)),
                "file_name": "a.pdf"}, raise_on_error=False)
    finally:
        lim.release()
    assert r1.is_error and "opacity" in r1.content[0].text
    assert r2.is_error and "{nope}" in r2.content[0].text
    assert r3.is_error and "server is busy" in r3.content[0].text


async def test_merge_with_bad_base64_watermark_image_is_refused():
    """TC-PDFPG-077"""
    async with _client() as client:
        r = await client.call_tool("pdf_watermark", {
            "watermark_image": b64(b"hello"), "file_content": b64(make_pdf(1)),
            "file_name": "a.pdf"}, raise_on_error=False)
    assert r.is_error and "not a PNG or JPEG" in r.content[0].text


def test_stamped_pages_reference_no_unused_standard_font():
    """TC-PDFPG-064: reportlab's initial Helvetica would be listed, never embedded."""
    data, _ = run(pp.add_page_numbers(make_pdf(1), pp.PageNumberOptions()))
    page = PdfReader(io.BytesIO(data)).pages[0]
    names = []

    def collect(resources):
        for ref in resources.get("/Font", {}).values():
            names.append(str(ref.get_object().get("/BaseFont")))
        for ref in resources.get("/XObject", {}).values():
            obj = ref.get_object()
            if "/Resources" in obj:
                collect(obj["/Resources"])
    collect(page["/Resources"])
    assert names and not any("Helvetica" in n for n in names), names
