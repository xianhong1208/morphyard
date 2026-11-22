"""PDF compress (#27), protect / unlock (#29), metadata read / strip (#30)."""

import base64
import io
import zlib

import pikepdf
import pytest
from fastmcp import Client, FastMCP
from pikepdf import Dictionary, Name, Pdf
from PIL import Image
from pypdf import PdfReader

from src.domain.convert import pdf_secure
from src.domain.convert.pdf_secure import PdfSecureError
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _photo(w: int, h: int, mode: str = "RGB") -> Image.Image:
    bands = [Image.linear_gradient("L").resize((w, h)),
             Image.radial_gradient("L").resize((w, h)),
             Image.effect_noise((w, h), 30)]
    img = Image.merge("RGB", bands)
    return img.convert(mode) if mode != "RGB" else img


def _image_xobject(pdf, pil: Image.Image, jpeg: bool = True, quality: int = 95):
    if jpeg:
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=quality)
        stream = pdf.make_stream(buf.getvalue())
        stream.Filter = Name.DCTDecode
    else:
        stream = pdf.make_stream(zlib.compress(pil.tobytes()))
        stream.Filter = Name.FlateDecode
    stream.Type = Name.XObject
    stream.Subtype = Name.Image
    stream.Width, stream.Height = pil.size
    stream.BitsPerComponent = 1 if pil.mode == "1" else 8
    stream.ColorSpace = {"RGB": Name.DeviceRGB, "L": Name.DeviceGray, "1": Name.DeviceGray,
                         "CMYK": Name.DeviceCMYK}[pil.mode]
    return stream


def _page(pdf, text: str = "Hello Morphyard", content_extra: bytes = b"",
          xobjects: dict | None = None):
    page = pdf.add_blank_page(page_size=(612, 792))
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    resources = Dictionary(Font=Dictionary(F1=font))
    if xobjects:
        resources.XObject = Dictionary({f"/{k}": v for k, v in xobjects.items()})
    page.obj.Resources = resources
    content = f"BT /F1 12 Tf 72 740 Td ({text}) Tj ET\n".encode() + content_extra
    page.obj.Contents = pdf.make_stream(content)
    return page


def _save(pdf, **kw) -> bytes:
    buf = io.BytesIO()
    pdf.save(buf, **kw)
    return buf.getvalue()


def _image_pdf(px=(900, 600), pt=(216, 144), jpeg=True, mode="RGB") -> bytes:
    """One page painting one image of px pixels over pt points (900 px / 3 in = 300 DPI)."""
    pdf = Pdf.new()
    im = _image_xobject(pdf, _photo(*px, mode=mode) if mode != "1"
                        else _photo(*px).convert("1"), jpeg=jpeg and mode != "1")
    _page(pdf, xobjects={"Im1": im},
          content_extra=f"q {pt[0]} 0 0 {pt[1]} 72 400 cm /Im1 Do Q".encode())
    return _save(pdf, compress_streams=False)


def _text_pdf(pages: int = 3) -> bytes:
    pdf = Pdf.new()
    for i in range(pages):
        _page(pdf, text=f"Page {i + 1} " + "lorem ipsum dolor sit amet " * 3)
    return _save(pdf, compress_streams=False, object_stream_mode=pikepdf.ObjectStreamMode.disable)


_OPEN: list = []  # keeps opened PDFs alive while a test looks at their objects


def _open(data: bytes, **kw):
    pdf = Pdf.open(io.BytesIO(data), **kw)
    _OPEN.append(pdf)
    return pdf


def _images(data: bytes) -> list:
    pdf = _open(data)
    return [o for o in pdf.objects
            if isinstance(o, pikepdf.Stream) and o.get("/Subtype") == "/Image"]


def _text(data: bytes) -> str:
    return "".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


def _encrypted(data: bytes, user="", owner="own", **allow) -> bytes:
    pdf = Pdf.open(io.BytesIO(data))
    perms = pikepdf.Permissions(**allow) if allow else pikepdf.Permissions(modify_assembly=True)
    return _save(pdf, encryption=pikepdf.Encryption(user=user, owner=owner, R=6, allow=perms))


# ---------------------------------------------------------------------------
# Compress
# ---------------------------------------------------------------------------

async def test_lossless_shrinks_uncompressed_streams_and_keeps_text():
    """TC-PDFSEC-001"""
    src = _text_pdf(20)
    r = await pdf_secure.compress_pdf(src, "lossless")
    assert not r.returned_original and r.output_bytes < len(src)
    assert _text(r.data) == _text(src)
    assert r.images_downsampled == r.images_recompressed == 0


async def test_balanced_downsamples_an_image_to_150_dpi():
    """TC-PDFSEC-002: 900 px over 3 in = 300 DPI -> 450 px."""
    src = _image_pdf()
    r = await pdf_secure.compress_pdf(src, "balanced")
    assert r.images_downsampled == 1 and not r.returned_original
    (img,) = _images(r.data)
    assert int(img.Width) == 450 and int(img.Height) == 300
    assert img.Filter == "/DCTDecode"
    assert r.output_bytes < len(src) / 2


async def test_small_is_smaller_than_balanced():
    """TC-PDFSEC-003"""
    src = _image_pdf()
    balanced = await pdf_secure.compress_pdf(src, "balanced")
    small = await pdf_secure.compress_pdf(src, "small")
    assert small.output_bytes < balanced.output_bytes
    (img,) = _images(small.data)
    assert int(img.Width) == 288  # 96 DPI over 3 in


async def test_not_smaller_returns_the_original_bytes():
    """TC-PDFSEC-004"""
    once = (await pdf_secure.compress_pdf(_text_pdf(1), "lossless")).data
    r = await pdf_secure.compress_pdf(once, "lossless")
    assert r.returned_original and r.data == once and r.output_bytes == len(once)


async def test_a_negligible_saving_returns_the_original(monkeypatch):
    """TC-PDFSEC-018: 28 bytes off an 11 MB scan is not worth a new file."""
    src = _text_pdf(20)
    monkeypatch.setattr(pdf_secure, "MIN_FILE_SAVING", 0.99)
    r = await pdf_secure.compress_pdf(src, "lossless")
    assert r.returned_original and r.data == src


@pytest.mark.parametrize("kwargs, needle", [
    ({"level": "tiny"}, "Unknown level"),
    ({"level": "lossless", "max_dpi": 100}, "only apply"),
    ({"level": "lossless", "jpeg_quality": 50}, "only apply"),
    ({"level": "balanced", "max_dpi": 20}, "max_dpi must be"),
    ({"level": "balanced", "max_dpi": 601}, "max_dpi must be"),
    ({"level": "balanced", "jpeg_quality": 5}, "jpeg_quality must be"),
    ({"level": "balanced", "jpeg_quality": 96}, "jpeg_quality must be"),
])
async def test_compress_rejects_bad_options(kwargs, needle):
    """TC-PDFSEC-005"""
    with pytest.raises(PdfSecureError, match=needle):
        await pdf_secure.compress_pdf(_text_pdf(1), **kwargs)


async def test_overrides_apply():
    """TC-PDFSEC-006: max_dpi=72 over 3 in -> 216 px."""
    r = await pdf_secure.compress_pdf(_image_pdf(), "balanced", max_dpi=72)
    (img,) = _images(r.data)
    assert int(img.Width) == 216


async def test_an_image_already_below_the_target_keeps_its_size():
    """TC-PDFSEC-007: 900 px over 7.5 in = 120 DPI < 150."""
    r = await pdf_secure.compress_pdf(_image_pdf(pt=(540, 360)), "balanced")
    assert r.images_downsampled == 0
    for img in _images(r.data):
        assert int(img.Width) == 900


@pytest.mark.parametrize("mode", ["1", "CMYK"])
async def test_bilevel_and_cmyk_images_are_left_alone(mode):
    """TC-PDFSEC-008"""
    src = _image_pdf(mode=mode)
    before = [bytes(i.read_raw_bytes()) for i in _images(src)]
    r = await pdf_secure.compress_pdf(src, "small")
    assert r.images_downsampled == r.images_recompressed == 0
    assert [bytes(i.read_raw_bytes()) for i in _images(r.data)] == before


async def test_dpi_follows_form_xobject_matrices():
    """TC-PDFSEC-009: an image inside a form scaled x2 is painted at 6 in -> 150 DPI, kept."""
    pdf = Pdf.new()
    im = _image_xobject(pdf, _photo(900, 600))
    form = pdf.make_stream(b"q 216 0 0 144 0 0 cm /Im1 Do Q")
    form.Type, form.Subtype = Name.XObject, Name.Form
    form.BBox = [0, 0, 216, 144]
    form.Matrix = [2, 0, 0, 2, 0, 0]
    form.Resources = Dictionary(XObject=Dictionary(Im1=im))
    _page(pdf, xobjects={"Fm1": form}, content_extra=b"q 1 0 0 1 72 100 cm /Fm1 Do Q")
    r = await pdf_secure.compress_pdf(_save(pdf), "balanced")
    assert r.images_downsampled == 0
    # without the matrix the image would count as 300 DPI and be halved
    r2 = await pdf_secure.compress_pdf(_save(pdf), "balanced", max_dpi=100)
    (img,) = _images(r2.data)
    assert int(img.Width) == 600  # 100 DPI over 6 in


async def test_too_many_pages_is_refused(monkeypatch):
    """TC-PDFSEC-010"""
    monkeypatch.setattr(pdf_secure, "MAX_PAGES", 2)
    with pytest.raises(PdfSecureError, match="limit is 2"):
        await pdf_secure.compress_pdf(_text_pdf(3), "lossless")


async def test_huge_images_are_skipped(monkeypatch):
    """TC-PDFSEC-011"""
    monkeypatch.setattr(pdf_secure, "MAX_IMAGE_PIXELS", 1000)
    r = await pdf_secure.compress_pdf(_image_pdf(), "balanced")
    assert r.images_downsampled == 0 and r.images_skipped == 1


async def test_image_count_cap(monkeypatch):
    """TC-PDFSEC-012"""
    monkeypatch.setattr(pdf_secure, "MAX_IMAGES", 0)
    r = await pdf_secure.compress_pdf(_image_pdf(), "balanced")
    assert r.images_downsampled == 0 and r.notes


@pytest.mark.parametrize("fn, kwargs", [
    (pdf_secure.compress_pdf, {}),
    (pdf_secure.strip_metadata, {}),
    (pdf_secure.protect_pdf, {"open_password": "x"}),
])
async def test_encrypted_input_is_refused(fn, kwargs):
    """TC-PDFSEC-013: a password-protected file and a restrictions-only file."""
    locked = _encrypted(_text_pdf(1), user="u", owner="o")
    with pytest.raises(PdfSecureError) as e:
        await fn(locked, **kwargs)
    assert e.value.error_code == "PDF_PASSWORD_REQUIRED" and "pdf_unlock" in e.value.message
    restricted = _encrypted(_text_pdf(1), owner="o", print_lowres=False, print_highres=False)
    with pytest.raises(PdfSecureError) as e:
        await fn(restricted, **kwargs)
    assert e.value.error_code == "PDF_ENCRYPTED" and "pdf_unlock" in e.value.message


async def test_a_flate_image_is_reencoded_only_when_much_smaller():
    """TC-PDFSEC-014: a flat-colour image compresses better as Flate; it stays."""
    pdf = Pdf.new()
    flat = Image.new("RGB", (300, 200), (200, 30, 30))
    im = _image_xobject(pdf, flat, jpeg=False)
    _page(pdf, xobjects={"Im1": im}, content_extra=b"q 216 0 0 144 72 400 cm /Im1 Do Q")
    r = await pdf_secure.compress_pdf(_save(pdf), "balanced")
    assert r.images_recompressed == 0
    (img,) = _images(r.data) if not r.returned_original else _images(_save(pdf))
    assert img.Filter == "/FlateDecode"


@pytest.mark.parametrize("key, value", [
    ("/Mask", [0, 10, 0, 10, 0, 10]),   # colour-key mask
    ("/Decode", [1, 0, 1, 0, 1, 0]),    # inverted samples
])
async def test_images_with_colour_key_masks_or_decode_arrays_are_left_alone(key, value):
    """TC-PDFSEC-016"""
    pdf = Pdf.new()
    im = _image_xobject(pdf, _photo(900, 600))
    im[key] = pikepdf.Array(value)
    _page(pdf, xobjects={"Im1": im}, content_extra=b"q 216 0 0 144 72 400 cm /Im1 Do Q")
    r = await pdf_secure.compress_pdf(_save(pdf), "small")
    assert r.images_downsampled == r.images_recompressed == 0


async def test_low_bit_depth_images_are_left_alone():
    """TC-PDFSEC-017: 2/4-bit images are line art or palettes; JPEG would smear them."""
    pdf = Pdf.new()
    w, h = 900, 600
    noise = _photo(w, h, mode="L").tobytes()
    packed = bytes((noise[i] & 0xF0) | (noise[i + 1] >> 4) for i in range(0, len(noise), 2))
    im = pdf.make_stream(zlib.compress(packed))
    im.Type, im.Subtype, im.Filter = Name.XObject, Name.Image, Name.FlateDecode
    im.Width, im.Height, im.BitsPerComponent, im.ColorSpace = w, h, 4, Name.DeviceGray
    _page(pdf, xobjects={"Im1": im}, content_extra=b"q 216 0 0 144 72 400 cm /Im1 Do Q")
    r = await pdf_secure.compress_pdf(_save(pdf), "small")
    assert r.images_downsampled == r.images_recompressed == 0


async def test_not_a_pdf_is_a_clear_error():
    """TC-PDFSEC-015"""
    with pytest.raises(PdfSecureError, match="not a readable PDF"):
        await pdf_secure.compress_pdf(b"%PDF-1.4\nnot really", "lossless")


# ---------------------------------------------------------------------------
# Protect
# ---------------------------------------------------------------------------

async def test_open_password_only_is_aes256_with_everything_allowed():
    """TC-PDFSEC-020"""
    out, info = await pdf_secure.protect_pdf(_text_pdf(1), open_password="pw")
    with pytest.raises(pikepdf.PasswordError):
        Pdf.open(io.BytesIO(out))
    with pytest.raises(pikepdf.PasswordError):
        Pdf.open(io.BytesIO(out), password="nope")
    pdf = Pdf.open(io.BytesIO(out), password="pw")
    assert pdf.encryption.stream_method.name == "aesv3" and pdf.encryption.bits == 256
    assert pdf.owner_password_matched  # owner = open password: nothing restricted
    assert info["restricted"] == []


async def test_restrictions_only_open_freely_and_set_the_bits():
    """TC-PDFSEC-021"""
    out, info = await pdf_secure.protect_pdf(
        _text_pdf(1), owner_password="boss",
        allow={"print": False, "copy": False})
    pdf = Pdf.open(io.BytesIO(out))
    assert pdf.is_encrypted and not pdf.owner_password_matched
    a = pdf.allow
    assert not a.print_lowres and not a.print_highres and not a.extract
    assert a.modify_other and a.modify_annotation and a.modify_form and a.accessibility
    assert Pdf.open(io.BytesIO(out), password="boss").owner_password_matched
    assert info["restricted"] == ["print", "copy"]


async def test_each_permission_flag_maps_to_its_bits():
    """TC-PDFSEC-022"""
    out, _ = await pdf_secure.protect_pdf(
        _text_pdf(1), owner_password="boss",
        allow={"modify": False, "annotate": False, "fill_forms": False})
    a = Pdf.open(io.BytesIO(out)).allow
    assert not a.modify_other and not a.modify_assembly
    assert not a.modify_annotation and not a.modify_form
    assert a.print_highres and a.extract


@pytest.mark.parametrize("kwargs, needle", [
    ({}, "Nothing to do"),
    ({"allow": {"print": False}}, "needs an owner password"),
    ({"open_password": "same", "owner_password": "same", "allow": {"copy": False}},
     "must differ"),
    ({"owner_password": "o", "allow": {"fill_forms": False}}, "annotate includes"),
    ({"open_password": "x", "allow": {"teleport": False}}, "Unknown permission"),
])
async def test_protect_rejects_unsafe_or_empty_requests(kwargs, needle):
    """TC-PDFSEC-023"""
    with pytest.raises(PdfSecureError, match=needle):
        await pdf_secure.protect_pdf(_text_pdf(1), **kwargs)


# ---------------------------------------------------------------------------
# Unlock
# ---------------------------------------------------------------------------

async def test_unlock_with_the_open_password():
    """TC-PDFSEC-030"""
    src = _text_pdf(2)
    locked, _ = await pdf_secure.protect_pdf(src, open_password="pw")
    out = await pdf_secure.unlock_pdf(locked, "pw")
    pdf = Pdf.open(io.BytesIO(out))
    assert not pdf.is_encrypted and len(pdf.pages) == 2
    assert _text(out) == _text(src)


async def test_wrong_password_gives_one_plain_message():
    """TC-PDFSEC-031: same text for a password-protected and a restrictions-only file."""
    locked, _ = await pdf_secure.protect_pdf(_text_pdf(1), open_password="pw")
    restricted, _ = await pdf_secure.protect_pdf(_text_pdf(1), owner_password="o",
                                                 allow={"print": False})
    messages = []
    for blob in (locked, restricted):
        with pytest.raises(PdfSecureError) as e:
            await pdf_secure.unlock_pdf(blob, "wrong")
        assert e.value.error_code == "PDF_WRONG_PASSWORD"
        messages.append(e.value.message)
    assert messages == [pdf_secure.WRONG_PASSWORD] * 2


async def test_open_password_does_not_lift_owner_restrictions():
    """TC-PDFSEC-032"""
    locked, _ = await pdf_secure.protect_pdf(
        _text_pdf(1), open_password="reader", owner_password="boss",
        allow={"print": False})
    with pytest.raises(PdfSecureError) as e:
        await pdf_secure.unlock_pdf(locked, "reader")
    assert e.value.error_code == "PDF_OWNER_PASSWORD_REQUIRED"
    out = await pdf_secure.unlock_pdf(locked, "boss")
    assert not Pdf.open(io.BytesIO(out)).is_encrypted


async def test_unlock_of_an_unencrypted_pdf_returns_none():
    """TC-PDFSEC-033"""
    assert await pdf_secure.unlock_pdf(_text_pdf(1), "anything") is None


async def test_unlock_needs_a_password():
    """TC-PDFSEC-034"""
    with pytest.raises(PdfSecureError, match="Give the PDF's password"):
        await pdf_secure.unlock_pdf(_text_pdf(1), "")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _rich_pdf() -> bytes:
    """Info, XMP (document + image), PieceInfo, annotations, JS, an attachment, a form."""
    pdf = Pdf.new()
    im = _image_xobject(pdf, _photo(64, 64))
    im.Metadata = pdf.make_stream(b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
    page = _page(pdf, xobjects={"Im1": im}, content_extra=b"q 64 0 0 64 72 400 cm /Im1 Do Q")
    page.obj.PieceInfo = Dictionary(Illustrator=Dictionary(Private=pikepdf.String("x")))
    page.obj.LastModified = pikepdf.String("D:20260101000000Z")
    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
        meta["dc:title"] = "Quarterly report"
        meta["dc:creator"] = ["Alice Example"]
        meta["xmp:CreatorTool"] = "Writer"
    pdf.docinfo["/Title"] = "Quarterly report"
    pdf.docinfo["/Author"] = "Alice Example"
    pdf.docinfo["/Producer"] = "Writer 1.0"
    js = pdf.make_indirect(Dictionary(S=Name.JavaScript, JS=pikepdf.String("app.alert(1)")))
    pdf.Root.OpenAction = js
    pdf.Root.AA = Dictionary(WC=Dictionary(S=Name.JavaScript, JS=pikepdf.String("x")))
    ef = pdf.make_stream(b"secret attachment")
    ef.Type = Name.EmbeddedFile
    spec = Dictionary(Type=Name.Filespec, F=pikepdf.String("notes.txt"),
                      EF=Dictionary(F=ef))
    pdf.Root.Names = Dictionary(
        JavaScript=Dictionary(Names=[pikepdf.String("init"), Dictionary(
            S=Name.JavaScript, JS=pikepdf.String("var a=1"))]),
        EmbeddedFiles=Dictionary(Names=[pikepdf.String("notes.txt"), spec]))
    note = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Text, Rect=[100, 100, 120, 120],
        Contents=pikepdf.String("Check this"), T=pikepdf.String("Bob Reviewer"),
        M=pikepdf.String("D:20260102000000Z")))
    link = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 10, 10],
        A=Dictionary(S=Name.JavaScript, JS=pikepdf.String("y"))))
    attach = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.FileAttachment, Rect=[0, 20, 10, 30], FS=spec))
    widget = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx, Rect=[200, 200, 300, 220],
        T=pikepdf.String("customer_name"), V=pikepdf.String("")))
    page.obj.Annots = pdf.make_indirect([note, link, attach, widget])
    pdf.Root.AcroForm = Dictionary(Fields=[widget])
    return _save(pdf)


async def test_metadata_report():
    """TC-PDFSEC-040"""
    r = await pdf_secure.read_metadata(_rich_pdf())
    assert r["page_count"] == 1 and r["pdf_version"].startswith("1.")
    assert r["encrypted"] is False
    assert r["info"]["Author"] == "Alice Example" and r["info"]["Producer"] == "Writer 1.0"
    assert r["has_xmp"] and r["xmp"]["dc:creator"] == ["Alice Example"]
    assert r["xmp"]["xmp:CreatorTool"] == "Writer"
    assert r["annotations"] == {"total": 4, "with_author": 1, "authors": ["Bob Reviewer"]}
    js = r["javascript"]
    assert js["document_scripts"] == ["init"] and js["open_action_is_javascript"]
    assert js["document_actions"] and js["annotations_with_actions"] == 1
    assert r["embedded_files"] == ["notes.txt"] and r["file_attachment_annotations"] == 1
    assert r["has_form"] and not r["has_xfa"]


async def test_metadata_of_a_password_protected_pdf():
    """TC-PDFSEC-041"""
    src = _rich_pdf()
    locked = _encrypted(src, user="pw", owner="own")
    bare = await pdf_secure.read_metadata(locked)
    assert bare["encrypted"] and bare["encryption"]["needs_open_password"]
    assert "password=" in bare["note"] and "info" not in bare
    full = await pdf_secure.read_metadata(locked, "pw")
    assert full["info"]["Author"] == "Alice Example"
    assert full["encryption"]["method"] == "AES-256"
    assert full["encryption"]["needs_open_password"] is True
    with pytest.raises(PdfSecureError) as e:
        await pdf_secure.read_metadata(locked, "bad")
    assert e.value.error_code == "PDF_WRONG_PASSWORD"


async def test_metadata_of_a_restrictions_only_pdf():
    """TC-PDFSEC-042"""
    restricted = _encrypted(_text_pdf(1), owner="o", extract=False)
    r = await pdf_secure.read_metadata(restricted)
    assert r["encrypted"] and r["encryption"]["needs_open_password"] is False
    assert r["encryption"]["permissions"]["extract"] is False
    assert r["encryption"]["permissions"]["print_highres"] is True


async def test_long_values_are_truncated():
    """TC-PDFSEC-043"""
    pdf = Pdf.open(io.BytesIO(_text_pdf(1)))
    pdf.docinfo["/Subject"] = "x" * 5000
    r = await pdf_secure.read_metadata(_save(pdf))
    assert len(r["info"]["Subject"]) == pdf_secure.MAX_VALUE_CHARS + 1


# ---------------------------------------------------------------------------
# Strip
# ---------------------------------------------------------------------------

def _all_keys(data: bytes) -> set[str]:
    pdf = Pdf.open(io.BytesIO(data))
    keys = set(pdf.trailer.keys())
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream):
            keys |= set(obj.stream_dict.keys())
        elif isinstance(obj, pikepdf.Dictionary):
            keys |= set(obj.keys())
    keys |= set(pdf.Root.keys())
    return keys


async def test_strip_removes_info_xmp_pieceinfo_and_keeps_content():
    """TC-PDFSEC-050"""
    src = _rich_pdf()
    r = await pdf_secure.strip_metadata(src)
    keys = _all_keys(r.data)
    assert "/Info" not in keys and "/Metadata" not in keys and "/PieceInfo" not in keys
    assert "/LastModified" not in keys
    assert b"Alice Example" not in r.data and b"Writer" not in r.data
    assert _text(r.data) == _text(src)
    assert r.removed["info_entries"] == 3 and r.removed["xmp_streams"] == 2
    assert bytes(_open(r.data).trailer.ID[0]) != bytes(_open(src).trailer.ID[0])
    after = await pdf_secure.read_metadata(r.data)
    assert after["info"] == {} and after["xmp"] == {} and not after["has_xmp"]


async def test_strip_annotation_authors_but_never_form_field_names():
    """TC-PDFSEC-051"""
    r = await pdf_secure.strip_metadata(_rich_pdf())
    annots = _open(r.data).pages[0].obj.Annots
    by_type = {str(a.Subtype): a for a in annots}
    assert "/T" not in by_type["/Text"] and "/M" not in by_type["/Text"]
    assert str(by_type["/Text"].Contents) == "Check this"
    assert str(by_type["/Widget"].T) == "customer_name"
    kept = await pdf_secure.strip_metadata(_rich_pdf(), remove_annotation_authors=False)
    assert b"Bob Reviewer" in bytes(_open(kept.data).pages[0].obj.Annots[0].T)


async def test_strip_javascript_only_when_asked():
    """TC-PDFSEC-052"""
    default = await pdf_secure.read_metadata(
        (await pdf_secure.strip_metadata(_rich_pdf())).data)
    assert default["javascript"]["open_action_is_javascript"]
    r = await pdf_secure.strip_metadata(_rich_pdf(), remove_javascript=True)
    after = await pdf_secure.read_metadata(r.data)
    js = after["javascript"]
    assert js["document_scripts"] == [] and not js["open_action_is_javascript"]
    assert not js["document_actions"] and js["annotations_with_actions"] == 0
    assert r.removed["javascript"] >= 4
    assert b"app.alert" not in r.data


async def test_strip_attachments_only_when_asked():
    """TC-PDFSEC-053"""
    default = await pdf_secure.read_metadata(
        (await pdf_secure.strip_metadata(_rich_pdf())).data)
    assert default["embedded_files"] == ["notes.txt"]
    assert default["file_attachment_annotations"] == 1
    r = await pdf_secure.strip_metadata(_rich_pdf(), remove_attachments=True)
    after = await pdf_secure.read_metadata(r.data)
    assert after["embedded_files"] == [] and after["file_attachment_annotations"] == 0
    assert after["annotations"]["total"] == 3
    assert b"secret attachment" not in r.data
    assert r.removed["embedded_files"] == 1 and r.removed["attachment_annotations"] == 1


# ---------------------------------------------------------------------------
# Through a real MCP client
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def _call(tool: str, args: dict, **kw):
    mcp = FastMCP("secure-test")
    register_morphyard_tools(mcp)
    async with Client(mcp) as client:
        return await client.call_tool(tool, args, raise_on_error=False, **kw)


def _file(result) -> bytes:
    return base64.b64decode(result.content[1].resource.blob)


async def test_tools_return_files_through_a_real_client():
    """TC-PDFSEC-060: list-returning tools need output_schema=None to get past FastMCP."""
    src = _image_pdf()
    r = await _call("pdf_compress", {"file_content": _b64(src), "file_name": "scan.pdf"})
    assert not r.is_error and "Compressed (balanced)" in r.content[0].text
    assert str(r.content[1].resource.uri).endswith("scan_compressed.pdf")
    assert r.content[1].resource.mime_type == "application/pdf"

    r = await _call("pdf_protect", {"file_content": _b64(src), "file_name": "a.pdf",
                                    "owner_password": "boss", "allow_copy": False})
    assert not r.is_error and "copy" in r.content[0].text
    protected = _file(r)
    assert not Pdf.open(io.BytesIO(protected)).allow.extract

    r = await _call("pdf_unlock", {"file_content": _b64(protected), "file_name": "a.pdf",
                                   "password": "boss"})
    assert not r.is_error and not Pdf.open(io.BytesIO(_file(r))).is_encrypted

    r = await _call("pdf_metadata", {"file_content": _b64(_rich_pdf()), "file_name": "a.pdf"})
    assert not r.is_error and r.structured_content["info"]["Author"] == "Alice Example"

    r = await _call("pdf_strip_metadata", {"file_content": _b64(_rich_pdf()),
                                           "file_name": "a.pdf"})
    assert not r.is_error and "info entries: 3" in r.content[0].text
    assert str(r.content[1].resource.uri).endswith("a_clean.pdf")


@pytest.mark.parametrize("tool, extra", [
    ("pdf_compress", {}), ("pdf_protect", {"password": "x"}),
    ("pdf_unlock", {"password": "x"}), ("pdf_strip_metadata", {}),
])
async def test_tools_refuse_a_non_pdf(tool, extra):
    """TC-PDFSEC-061"""
    r = await _call(tool, {"file_content": _b64(b"hello world, plain text"),
                           "file_name": "a.txt", **extra})
    assert not r.is_error and r.content[0].text.startswith("This is not a PDF")


async def test_compress_tool_says_when_it_returns_the_original():
    """TC-PDFSEC-062"""
    once = (await pdf_secure.compress_pdf(_text_pdf(1), "lossless")).data
    r = await _call("pdf_compress", {"file_content": _b64(once), "file_name": "a.pdf",
                                     "level": "lossless"})
    assert r.content[0].text.startswith("Not compressed")
    assert _file(r) == once
    assert str(r.content[1].resource.uri).endswith("/a.pdf")


async def test_wrong_password_is_a_tool_error_with_the_plain_message():
    """TC-PDFSEC-063"""
    locked, _ = await pdf_secure.protect_pdf(_text_pdf(1), open_password="pw")
    r = await _call("pdf_unlock", {"file_content": _b64(locked), "file_name": "a.pdf",
                                   "password": "guess"})
    assert r.is_error and pdf_secure.WRONG_PASSWORD in r.content[0].text


async def test_unlock_tool_on_an_unencrypted_pdf_returns_only_a_note():
    """TC-PDFSEC-064"""
    r = await _call("pdf_unlock", {"file_content": _b64(_text_pdf(1)), "file_name": "a.pdf",
                                   "password": "x"})
    assert not r.is_error and len(r.content) == 1
    assert r.content[0].text == "This PDF is not encrypted; there is nothing to unlock."


async def test_pdf_protect_keeps_its_old_call_shape():
    """TC-PDFSEC-065: password alone still means 'add an open password'."""
    r = await _call("pdf_protect", {"file_content": _b64(_text_pdf(1)), "file_name": "a.pdf",
                                    "password": "pw"})
    assert not r.is_error and str(r.content[1].resource.uri).endswith("a_protected.pdf")
    with pytest.raises(pikepdf.PasswordError):
        Pdf.open(io.BytesIO(_file(r)))


@pytest.fixture
def _restore_limiter():
    from src.domain.convert import limiter as limiter_mod
    saved = limiter_mod._limiter
    yield limiter_mod
    limiter_mod._limiter = saved


@pytest.mark.parametrize("tool, extra", [
    ("pdf_compress", {}), ("pdf_protect", {"password": "x"}),
    ("pdf_unlock", {"password": "x"}), ("pdf_strip_metadata", {}),
])
async def test_rewriting_tools_take_a_slot_and_metadata_does_not(tool, extra, _restore_limiter):
    """TC-PDFSEC-066"""
    lim = _restore_limiter.configure(1, 0, 30)
    src = _b64(_text_pdf(1))
    await lim.acquire()  # every slot taken, no queue
    try:
        busy = await _call(tool, {"file_content": src, "file_name": "a.pdf", **extra})
        meta = await _call("pdf_metadata", {"file_content": src, "file_name": "a.pdf"})
    finally:
        lim.release()
    assert busy.is_error and "server is busy" in busy.content[0].text
    assert not meta.is_error
