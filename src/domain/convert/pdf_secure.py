"""PDF security and housekeeping operations on pikepdf (qpdf): compress, protect
with an open password and/or permissions, unlock, read metadata, strip metadata.

Kept apart from pdfops.py (pypdf) because these all need qpdf's object-level
access: AES-256 with real permission bits, object streams, removing
unreferenced objects, walking every object to drop metadata.

Everything runs in memory: no temp files are written. Passwords are never
logged, and neither is ``Pdf.encryption`` (its repr contains the user password).

Encrypted input is refused by every operation except unlock (and the read-only
metadata view, which reports the encryption itself), with a message that points
at pdf_unlock.
"""

from __future__ import annotations

import asyncio
import io
import math
import warnings
from dataclasses import dataclass, field
from typing import Any

from src.domain.exceptions import DomainException
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# Limits. Inputs are already capped at convert.max_input_mb (50 MB by default),
# but a small PDF can still hold a huge number of pages or a decompression bomb.
MAX_PAGES = 5000
MAX_IMAGES = 2000                     # images considered for re-encoding per document
MAX_IMAGE_PIXELS = 100_000_000        # larger images are left alone (memory)
MIN_IMAGE_SAVING = 0.10               # a re-encoded image must be >= 10 % smaller to be kept
MIN_FILE_SAVING = 0.01                # below 1 % saved, the original file is returned
MAX_LIST = 50                         # entries per list in the metadata report
MAX_VALUE_CHARS = 300                 # characters per value in the metadata report

COMPRESS_LEVELS = {
    # level: (max image DPI, JPEG quality); None = images untouched
    "lossless": None,
    "balanced": (150, 75),
    "small": (96, 50),
}


class PdfSecureError(DomainException):
    """A request this module refuses, with a message written for the caller."""

    def __init__(self, message: str, error_code: str = "PDF_INVALID_REQUEST"):
        super().__init__(message=message, error_code=error_code)


PASSWORD_REQUIRED = (
    "This PDF is password-protected. Ask the user for its password and call "
    "pdf_unlock first, then run this operation on the unlocked file."
)
ENCRYPTED_INPUT = (
    "This PDF is encrypted (it has an owner password and/or permission restrictions). "
    "Remove the encryption with pdf_unlock (it needs the owner password) and run this "
    "operation on the unlocked file."
)
WRONG_PASSWORD = "The password is not correct for this PDF."


def _pikepdf():
    try:
        import pikepdf
        return pikepdf
    except ImportError:  # pragma: no cover - it is a hard dependency
        raise PdfSecureError("pikepdf is not installed on this server", "PDF_ENGINE_MISSING")


def _open(data: bytes, password: str = ""):
    """Open a PDF. Translates pikepdf's errors into messages a caller can act on."""
    pikepdf = _pikepdf()
    try:
        with warnings.catch_warnings():
            # "A password was provided, but no password was needed": expected for an
            # unencrypted or restrictions-only file; the callers check themselves.
            warnings.simplefilter("ignore", UserWarning)
            return pikepdf.Pdf.open(io.BytesIO(data), password=password or "")
    except pikepdf.PasswordError:
        if password:
            raise PdfSecureError(WRONG_PASSWORD, "PDF_WRONG_PASSWORD")
        raise PdfSecureError(PASSWORD_REQUIRED, "PDF_PASSWORD_REQUIRED")
    except pikepdf.PdfError as e:
        raise PdfSecureError(f"This file is not a readable PDF: {str(e)[:300]}", "PDF_UNREADABLE")


def _open_unencrypted(data: bytes):
    pdf = _open(data)
    if pdf.is_encrypted:
        pdf.close()
        raise PdfSecureError(ENCRYPTED_INPUT, "PDF_ENCRYPTED")
    if len(pdf.pages) > MAX_PAGES:
        n = len(pdf.pages)
        pdf.close()
        raise PdfSecureError(
            f"This PDF has {n} pages; the limit is {MAX_PAGES}. Use pdf_extract_pages "
            "to work on part of it.", "PDF_TOO_MANY_PAGES",
        )
    return pdf


def _save(pdf, **kwargs) -> bytes:
    buf = io.BytesIO()
    pdf.save(buf, **kwargs)
    return buf.getvalue()


# ============================================================================
# Compress (#27)
# ============================================================================

@dataclass
class CompressResult:
    data: bytes
    original_bytes: int
    output_bytes: int
    returned_original: bool
    level: str
    images_recompressed: int = 0
    images_downsampled: int = 0
    images_skipped: int = 0
    notes: list[str] = field(default_factory=list)


def _mul(m1, m2):
    """m1 x m2 for PDF matrices [a b c d e f] (row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2,
    )


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _image_display_sizes(pdf) -> dict[tuple[int, int], tuple[float, float]]:
    """Largest size, in points, at which each image XObject is painted.

    Walks every page's content stream (and form XObjects inside it) tracking the
    current transformation matrix, so the effective DPI of an image is its pixel
    size over the area it really covers on the page. Images painted only from
    places this does not walk (patterns, annotation appearances) are absent from
    the result and are left at their resolution.
    """
    pikepdf = _pikepdf()
    sizes: dict[tuple[int, int], tuple[float, float]] = {}

    def walk(ops, resources, ctm, depth, path):
        stack = []
        xobjects = resources.get("/XObject") if resources is not None else None
        for operands, operator in ops:
            op = str(operator)
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                ctm = stack.pop() if stack else ctm
            elif op == "cm" and len(operands) == 6:
                ctm = _mul(tuple(float(x) for x in operands), ctm)
            elif op == "Do" and operands and xobjects is not None:
                xobj = xobjects.get(operands[0])
                if not isinstance(xobj, pikepdf.Stream):
                    continue
                subtype = xobj.get("/Subtype")
                if subtype == "/Image":
                    a, b, c, d, _, _ = ctm
                    w, h = math.hypot(a, b), math.hypot(c, d)
                    key = xobj.objgen
                    old = sizes.get(key, (0.0, 0.0))
                    sizes[key] = (max(old[0], w), max(old[1], h))
                elif subtype == "/Form" and depth < 8 and xobj.objgen not in path:
                    matrix = xobj.get("/Matrix")
                    m = tuple(float(x) for x in matrix) if matrix is not None else _IDENTITY
                    try:
                        inner = pikepdf.parse_content_stream(xobj)
                    except Exception:
                        continue
                    walk(inner, xobj.get("/Resources", resources), _mul(m, ctm),
                         depth + 1, path | {xobj.objgen})

    for page_no, page in enumerate(pdf.pages, start=1):
        try:
            ops = pikepdf.parse_content_stream(page)
        except Exception as e:
            logger.warning(f"[pikepdf] page {page_no}: content not parsed for image sizes: {e}")
            continue
        walk(ops, page.obj.get("/Resources"), _IDENTITY, 0, frozenset())
    return sizes


def _recompress_image(pikepdf, image, display, max_dpi: int, quality: int):
    """Re-encode one image XObject as JPEG, downsampled to max_dpi.

    Returns "downsampled" / "recompressed" when the stream was replaced, or None
    when the image was left as it was (ineligible, or not enough saved).
    """
    from PIL import Image

    if image.get("/ImageMask", False) or image.get("/BitsPerComponent") != 8:
        return None
    if "/Decode" in image or isinstance(image.get("/Mask"), pikepdf.Array):
        return None  # colour-key masks / decode arrays would not survive a JPEG
    width, height = int(image.Width), int(image.Height)
    if width * height > MAX_IMAGE_PIXELS or width < 16 or height < 16:
        return None

    pil = pikepdf.PdfImage(image).as_pil_image()
    colorspace = image.get("/ColorSpace")
    keep_colorspace = None
    if pil.mode == "P":
        pil = pil.convert("RGB")
    elif pil.mode in ("RGB", "L"):
        # Keep an ICC profile whose channel count still matches the pixels
        if isinstance(colorspace, pikepdf.Array) and len(colorspace) == 2 \
                and colorspace[0] == "/ICCBased":
            keep_colorspace = colorspace
    else:
        return None  # CMYK, 1-bit, LA...: not worth the colour risk

    action = "recompressed"
    w_pt, h_pt = display
    if w_pt > 0 and h_pt > 0:
        dpi = min(width * 72.0 / w_pt, height * 72.0 / h_pt)
        if dpi > max_dpi * 1.1:
            scale = max_dpi / dpi
            new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            pil = pil.resize(new_size, Image.Resampling.LANCZOS)
            action = "downsampled"

    out = io.BytesIO()
    pil.save(out, format="JPEG", quality=quality, optimize=True)
    jpeg = out.getvalue()
    if len(jpeg) > len(image.read_raw_bytes()) * (1 - MIN_IMAGE_SAVING):
        return None

    image.write(jpeg, filter=pikepdf.Name.DCTDecode)
    image.Width, image.Height = pil.size
    image.BitsPerComponent = 8
    image.ColorSpace = keep_colorspace if keep_colorspace is not None else (
        pikepdf.Name.DeviceRGB if pil.mode == "RGB" else pikepdf.Name.DeviceGray)
    for key in ("/DecodeParms", "/SMaskInData", "/Interpolate"):
        if key in image:
            del image[key]
    return action


def _compress_sync(data: bytes, level: str, max_dpi: int | None,
                   jpeg_quality: int | None) -> CompressResult:
    pikepdf = _pikepdf()
    pdf = _open_unencrypted(data)
    result = CompressResult(data=data, original_bytes=len(data), output_bytes=len(data),
                            returned_original=True, level=level)
    try:
        settings = COMPRESS_LEVELS[level]
        if settings is not None:
            dpi = max_dpi or settings[0]
            quality = jpeg_quality or settings[1]
            sizes = _image_display_sizes(pdf)
            seen = 0
            for obj in pdf.objects:
                if not (isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image"):
                    continue
                seen += 1
                if seen > MAX_IMAGES:
                    result.notes.append(f"only the first {MAX_IMAGES} images were considered")
                    break
                try:
                    action = _recompress_image(pikepdf, obj, sizes.get(obj.objgen, (0, 0)),
                                               dpi, quality)
                except Exception as e:  # one odd image must not fail the document
                    logger.warning(f"[pikepdf] image {obj.objgen} left as is: {e}")
                    action = None
                if action == "downsampled":
                    result.images_downsampled += 1
                elif action == "recompressed":
                    result.images_recompressed += 1
                else:
                    result.images_skipped += 1

        pdf.remove_unreferenced_resources()
        out = _save(
            pdf,
            compress_streams=True,
            recompress_flate=True,
            stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
    finally:
        pdf.close()

    if len(out) <= len(data) * (1 - MIN_FILE_SAVING):
        result.data = out
        result.output_bytes = len(out)
        result.returned_original = False
    return result


async def compress_pdf(data: bytes, level: str = "balanced", max_dpi: int | None = None,
                       jpeg_quality: int | None = None) -> CompressResult:
    """Make a PDF smaller. If the result is not smaller, the original comes back."""
    level = (level or "balanced").strip().lower()
    if level not in COMPRESS_LEVELS:
        raise PdfSecureError(
            f"Unknown level '{level}'. Use one of: {', '.join(COMPRESS_LEVELS)}.")
    if COMPRESS_LEVELS[level] is None and (max_dpi or jpeg_quality):
        raise PdfSecureError(
            "max_dpi and jpeg_quality only apply to the 'balanced' and 'small' levels; "
            "'lossless' never touches images.")
    if max_dpi is not None and max_dpi and not 36 <= max_dpi <= 600:
        raise PdfSecureError("max_dpi must be between 36 and 600.")
    if jpeg_quality is not None and jpeg_quality and not 10 <= jpeg_quality <= 95:
        raise PdfSecureError("jpeg_quality must be between 10 and 95.")

    result = await asyncio.to_thread(_compress_sync, data, level, max_dpi, jpeg_quality)
    logger.info(
        f"[pikepdf] compress {level}: {result.original_bytes} -> {result.output_bytes} bytes"
        f" (downsampled {result.images_downsampled}, re-encoded {result.images_recompressed},"
        f" original returned: {result.returned_original})"
    )
    return result


# ============================================================================
# Protect: open password and/or permissions (#29)
# ============================================================================

PERMISSION_FLAGS = ("print", "copy", "modify", "annotate", "fill_forms")


def _permissions(pikepdf, allow: dict[str, bool]):
    """Map the five caller-facing flags onto the PDF permission bits.

    accessibility (text extraction for screen readers) is always left on.
    """
    return pikepdf.Permissions(
        accessibility=True,
        print_lowres=allow["print"],
        print_highres=allow["print"],
        extract=allow["copy"],
        modify_other=allow["modify"],
        modify_assembly=allow["modify"],
        modify_annotation=allow["annotate"],
        modify_form=allow["fill_forms"] or allow["annotate"],
    )


async def protect_pdf(data: bytes, open_password: str = "", owner_password: str = "",
                      allow: dict[str, bool] | None = None) -> tuple[bytes, dict[str, Any]]:
    """Encrypt with AES-256: an open password, permission restrictions, or both.

    - Only an open password: owner password = open password, nothing restricted
      (whoever can open the file may do everything).
    - Restrictions: an owner password is required and must differ from the open
      password, otherwise anyone who can open the file could lift them.
    """
    allow = {k: True for k in PERMISSION_FLAGS} | (allow or {})
    unknown = set(allow) - set(PERMISSION_FLAGS)
    if unknown:
        raise PdfSecureError(f"Unknown permission(s): {', '.join(sorted(unknown))}.")
    restricted = [k for k in PERMISSION_FLAGS if not allow[k]]

    if not open_password and not restricted:
        raise PdfSecureError(
            "Nothing to do: give an open password, or switch off at least one permission "
            "(with an owner password).")
    if restricted and not owner_password:
        raise PdfSecureError(
            "Restricting permissions needs an owner password (the password that can later "
            "lift the restrictions). Ask the user for one.")
    if allow["annotate"] and not allow["fill_forms"]:
        raise PdfSecureError(
            "In a PDF, permission to annotate includes filling in forms. To block form "
            "filling, set allow_annotate to false as well.")
    if restricted and owner_password == open_password:
        raise PdfSecureError(
            "The owner password must differ from the open password; otherwise anyone who "
            "can open the file can also remove the restrictions.")

    def _run() -> bytes:
        pikepdf = _pikepdf()
        pdf = _open_unencrypted(data)
        try:
            enc = pikepdf.Encryption(
                user=open_password or "",
                owner=owner_password or open_password,
                R=6,  # AES-256
                allow=_permissions(pikepdf, allow),
            )
            return _save(pdf, encryption=enc)
        finally:
            pdf.close()

    out = await asyncio.to_thread(_run)
    summary = {
        "open_password": bool(open_password),
        "owner_password": bool(owner_password) or bool(open_password),
        "restricted": restricted,
        "encryption": "AES-256",
    }
    logger.info(f"[pikepdf] protect: open password {summary['open_password']}, "
                f"restricted {restricted}")
    return out, summary


# ============================================================================
# Unlock (#29)
# ============================================================================

def _all_allowed(perms) -> bool:
    return all(getattr(perms, name) for name in perms._fields)


async def unlock_pdf(data: bytes, password: str) -> bytes | None:
    """Remove the encryption, given the password. None when the file is not encrypted.

    The owner password always unlocks. The open (user) password unlocks only a
    file without permission restrictions; if the owner restricted the file, its
    restrictions are not lifted with the open password alone.
    """
    if not password:
        raise PdfSecureError("Give the PDF's password (ask the user for it).")

    def _run() -> bytes | None:
        pdf = _open(data, password)
        try:
            if not pdf.is_encrypted:
                return None
            if not pdf.owner_password_matched and not _all_allowed(pdf.allow):
                raise PdfSecureError(
                    "This password opens the PDF, but the owner restricted what readers may "
                    "do with it. Removing the encryption needs the owner password.",
                    "PDF_OWNER_PASSWORD_REQUIRED",
                )
            return _save(pdf, encryption=False)
        finally:
            pdf.close()

    out = await asyncio.to_thread(_run)
    logger.info(f"[pikepdf] unlock: {'not encrypted' if out is None else 'decrypted'}")
    return out


# ============================================================================
# Metadata read / strip (#30)
# ============================================================================

_XMP_PREFIXES = {
    "http://purl.org/dc/elements/1.1/": "dc",
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/pdf/1.3/": "pdf",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://ns.adobe.com/xap/1.0/rights/": "xmpRights",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://www.aiim.org/pdfa/ns/id/": "pdfaid",
    "http://ns.adobe.com/pdfx/1.3/": "pdfx",
}


def _short(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return [_short(v) for v in list(value)[:MAX_LIST]]
    text = str(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"


def _qname(clark: str) -> str:
    if clark.startswith("{") and "}" in clark:
        uri, local = clark[1:].split("}", 1)
        return f"{_XMP_PREFIXES.get(uri, uri)}:{local}"
    return clark


def _names_tree_keys(pikepdf, pdf, tree_name: str) -> list[str]:
    names = pdf.Root.get("/Names")
    if names is None or tree_name not in names:
        return []
    try:
        return [str(k) for k in pikepdf.NameTree(names[tree_name]).keys()]
    except Exception:
        return ["(unreadable)"]


def _is_js_action(action) -> bool:
    try:
        return action is not None and action.get("/S") == "/JavaScript"
    except Exception:
        return False


def _opens_without_password(pikepdf, data: bytes) -> bool:
    try:
        pikepdf.Pdf.open(io.BytesIO(data)).close()
        return True
    except pikepdf.PasswordError:
        return False


def _metadata_sync(data: bytes, password: str) -> dict[str, Any]:
    pikepdf = _pikepdf()
    try:
        pdf = _open(data, password)
    except PdfSecureError as e:
        if e.error_code != "PDF_PASSWORD_REQUIRED":
            raise
        # Without the password nothing inside can be read, not even the Info
        # dictionary (its strings are encrypted). Say so rather than fail.
        return {
            "encrypted": True,
            "encryption": {"needs_open_password": True},
            "note": "This PDF needs a password to open. Call pdf_metadata again with "
                    "password=<the user's password> to read its metadata.",
        }
    try:
        report: dict[str, Any] = {
            "pdf_version": pdf.pdf_version,
            "page_count": len(pdf.pages),
            "encrypted": pdf.is_encrypted,
        }
        if pdf.is_encrypted:
            enc = pdf.encryption
            report["encryption"] = {
                "method": {"aesv3": "AES-256", "aesv2": "AES-128", "rc4": "RC4"}.get(
                    enc.stream_method.name, enc.stream_method.name),
                "needs_open_password": not _opens_without_password(pikepdf, data),
                "permissions": {k: getattr(pdf.allow, k) for k in pdf.allow._fields},
            }

        info = pdf.trailer.get("/Info")
        report["info"] = {str(k).lstrip("/"): _short(v) for k, v in
                          list(info.items())[:MAX_LIST]} if info is not None else {}

        xmp: dict[str, Any] = {}
        has_xmp = "/Metadata" in pdf.Root
        if has_xmp:
            try:
                meta = pdf.open_metadata()
                for key in list(meta)[:MAX_LIST]:
                    xmp[_qname(key)] = _short(meta[key])
            except Exception as e:
                xmp = {"error": f"XMP not readable: {str(e)[:200]}"}
        report["xmp"] = xmp
        report["has_xmp"] = has_xmp

        annots_total = with_author = js_annots = attach_annots = 0
        authors: list[str] = []
        page_actions = 0
        for page in pdf.pages:
            if "/AA" in page.obj:
                page_actions += 1
            for annot in page.obj.get("/Annots", []) or []:
                if not isinstance(annot, pikepdf.Dictionary):
                    continue
                annots_total += 1
                if annot.get("/Subtype") == "/FileAttachment":
                    attach_annots += 1
                if _is_js_action(annot.get("/A")) or "/AA" in annot:
                    js_annots += 1
                if annot.get("/Subtype") != "/Widget" and "/T" in annot:
                    with_author += 1
                    name = _short(annot.T)
                    if name not in authors and len(authors) < 20:
                        authors.append(name)
        report["annotations"] = {"total": annots_total, "with_author": with_author,
                                 "authors": authors}

        open_action = pdf.Root.get("/OpenAction")
        report["javascript"] = {
            "document_scripts": _names_tree_keys(pikepdf, pdf, "/JavaScript")[:MAX_LIST],
            "open_action_is_javascript": isinstance(open_action, pikepdf.Dictionary)
            and _is_js_action(open_action),
            "document_actions": "/AA" in pdf.Root,
            "pages_with_actions": page_actions,
            "annotations_with_actions": js_annots,
        }
        report["embedded_files"] = _names_tree_keys(pikepdf, pdf, "/EmbeddedFiles")[:MAX_LIST]
        report["file_attachment_annotations"] = attach_annots
        acroform = pdf.Root.get("/AcroForm")
        report["has_form"] = acroform is not None
        report["has_xfa"] = acroform is not None and "/XFA" in acroform
        return report
    finally:
        pdf.close()


async def read_metadata(data: bytes, password: str = "") -> dict[str, Any]:
    """Read-only report: Info, XMP summary, version, pages, encryption, scripts, files."""
    return await asyncio.to_thread(_metadata_sync, data, password)


@dataclass
class StripResult:
    data: bytes
    removed: dict[str, int]


def _strip_sync(data: bytes, remove_annotation_authors: bool, remove_javascript: bool,
                remove_attachments: bool) -> StripResult:
    pikepdf = _pikepdf()
    pdf = _open_unencrypted(data)
    removed = {"info_entries": 0, "xmp_streams": 0, "piece_info": 0, "annotation_authors": 0,
               "javascript": 0, "embedded_files": 0, "attachment_annotations": 0}
    try:
        info = pdf.trailer.get("/Info")
        if info is not None:
            removed["info_entries"] = len(info.keys())
            del pdf.trailer["/Info"]

        # XMP on the catalog, pages, images, fonts... and app-private PieceInfo
        for obj in pdf.objects:
            if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                continue
            target = obj.stream_dict if isinstance(obj, pikepdf.Stream) else obj
            if "/Metadata" in target:
                del target["/Metadata"]
                removed["xmp_streams"] += 1
            if "/PieceInfo" in target:
                del target["/PieceInfo"]
                removed["piece_info"] += 1
                if "/LastModified" in target:
                    del target["/LastModified"]
        if "/Metadata" in pdf.Root:  # the catalog is a direct object in some files
            del pdf.Root["/Metadata"]
            removed["xmp_streams"] += 1

        names = pdf.Root.get("/Names")
        if remove_javascript:
            if names is not None and "/JavaScript" in names:
                removed["javascript"] += max(1, len(_names_tree_keys(pikepdf, pdf, "/JavaScript")))
                del names["/JavaScript"]
            if _is_js_action(pdf.Root.get("/OpenAction")):
                del pdf.Root["/OpenAction"]
                removed["javascript"] += 1
            for obj in pdf.objects:
                if not isinstance(obj, pikepdf.Dictionary):
                    continue
                if "/AA" in obj:
                    del obj["/AA"]
                    removed["javascript"] += 1
                if _is_js_action(obj.get("/A")):
                    del obj["/A"]
                    removed["javascript"] += 1
            if "/AA" in pdf.Root:
                del pdf.Root["/AA"]
                removed["javascript"] += 1

        if remove_attachments:
            if names is not None and "/EmbeddedFiles" in names:
                removed["embedded_files"] = len(_names_tree_keys(pikepdf, pdf, "/EmbeddedFiles"))
                del names["/EmbeddedFiles"]
            for holder in [pdf.Root] + [p.obj for p in pdf.pages]:
                if "/AF" in holder:
                    del holder["/AF"]

        for page in pdf.pages:
            annots = page.obj.get("/Annots")
            if annots is None:
                continue
            keep = []
            for annot in annots:
                if remove_attachments and isinstance(annot, pikepdf.Dictionary) \
                        and annot.get("/Subtype") == "/FileAttachment":
                    removed["attachment_annotations"] += 1
                    continue
                if remove_annotation_authors and isinstance(annot, pikepdf.Dictionary) \
                        and annot.get("/Subtype") != "/Widget":
                    # /T on a markup annotation is its author; on a form widget it is
                    # the field name, which must stay.
                    if "/T" in annot:
                        del annot["/T"]
                        removed["annotation_authors"] += 1
                    for key in ("/M", "/CreationDate"):
                        if key in annot:
                            del annot[key]
                keep.append(annot)
            if len(keep) != len(annots):
                page.obj.Annots = pikepdf.Array(keep)

        pdf.remove_unreferenced_resources()
        # A fresh /ID derived from the content, so the output no longer carries the
        # original file's identifier (qpdf keeps the first half of an existing one).
        if "/ID" in pdf.trailer:
            del pdf.trailer["/ID"]
        out = _save(pdf, deterministic_id=True,
                    object_stream_mode=pikepdf.ObjectStreamMode.generate)
    finally:
        pdf.close()
    return StripResult(data=out, removed=removed)


async def strip_metadata(data: bytes, remove_annotation_authors: bool = True,
                         remove_javascript: bool = False,
                         remove_attachments: bool = False) -> StripResult:
    """Remove document metadata; optionally scripts, attachments, annotation authors."""
    result = await asyncio.to_thread(_strip_sync, data, remove_annotation_authors,
                                     remove_javascript, remove_attachments)
    logger.info(f"[pikepdf] strip metadata: {result.removed}")
    return result
