"""Several files per call, store_file and signed upload URLs (issue #36).

Covers the `files` input of pdf_merge and images_to_pdf (one FileInput shape: file_content or
file_id), the `store_file` path for hosts that inject one upload per call, `create_upload` +
`PUT /uploads/{file_id}` (signature, expiry, single use, size cap on the stream, store quota,
owner binding) and the clean-up of incomplete uploads.
"""

import asyncio
import base64
import io
import logging
import time
import zipfile
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from PIL import Image
from pypdf import PdfReader, PdfWriter
from starlette.testclient import TestClient

from src.api.router.files import _RedactFileQueries
from src.api.router.uploads import router as uploads_router
from src.domain.files import FilesRuntime, LinkSigner, LocalStore, StoreFullError, Uploads, set_runtime
from src.domain.files import uploads as uploads_mod
from src.domain.operations.sources import ANONYMOUS_OWNER
from src.fastmcp_tools import file_inputs, operation_tool
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

KEY = b"u" * 32
MB = 1024 * 1024


def make_pdf(n=1, width=200) -> bytes:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width, 300)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def image(color, size=(40, 20), fmt="PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format=fmt)
    return out.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _runtime(tmp_path, mode="link", **store_kw) -> FilesRuntime:
    store = LocalStore(tmp_path / "store", **store_kw)
    signer = LinkSigner(KEY, "https://files.example")
    rt = FilesRuntime(store=store, signer=signer, sweep_interval_sec=60, result_delivery=mode,
                      inline_max_bytes=10 * MB, uploads=Uploads(tmp_path / "uploads", signer))
    set_runtime(rt)
    return rt


def _client():
    mcp = FastMCP("uploads-test")
    register_morphyard_tools(mcp)
    return Client(mcp)


def _http():
    app = FastAPI()
    app.include_router(uploads_router)
    return TestClient(app)


def _path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + "?" + parts.query


def _url_from(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("https://"))


def _file_id_of(text: str) -> str:
    return text.split("file_id ")[1].split()[0].rstrip(",:.")


class Token:
    def __init__(self, sub):
        self.claims = {"sub": sub}
        self.client_id = "c1"


@pytest.fixture
def as_user(monkeypatch):
    who = {"token": None}
    monkeypatch.setattr(operation_tool, "get_access_token", lambda: who["token"])

    def switch(sub):
        who["token"] = Token(sub) if sub else None
    return switch


def _merged_pages(result) -> PdfReader:
    stored = _file_id_of(result.content[0].text)
    from src.domain.files import get_store
    with get_store().open(stored) as f:
        return PdfReader(io.BytesIO(f.read()))


# ---------------------------------------------------------------------------- store_file

async def test_injected_uploads_stored_one_by_one_then_merged(tmp_path):
    """TC-UPL-001: a host that injects each upload into file_content only: three store_file
    calls give three file_ids, and one pdf_merge with files=[{file_id}...] merges them in
    order, each stored name becoming a bookmark."""
    _runtime(tmp_path)
    ids = []
    async with _client() as client:
        for i, pages in enumerate((1, 2, 3), start=1):
            stored = await client.call_tool("store_file", {
                "file_content": b64(make_pdf(pages, 100 + i)), "file_name": f"Part {i}.pdf"})
            text = stored.content[0].text
            assert "a PDF file" in text and "kept until" in text
            ids.append(_file_id_of(text))
        merged = await client.call_tool("pdf_merge", {"files": [{"file_id": i} for i in ids]})
    assert not merged.is_error, merged.content[0].text
    reader = _merged_pages(merged)
    assert [p.mediabox.width for p in reader.pages] == [101, 102, 102, 103, 103, 103]
    assert [o.title for o in reader.outline] == ["Part 1", "Part 2", "Part 3"]


async def test_store_file_keeps_any_file_as_is(tmp_path):
    """TC-UPL-002: store_file stores bytes unchanged, also a format Morphyard does not
    convert (a ZIP), as kind "upload"; without a name it is called file[.ext]."""
    rt = _runtime(tmp_path)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.png", image((1, 2, 3)))
    async with _client() as client:
        zipped = await client.call_tool("store_file", {"file_content": b64(archive.getvalue()),
                                                       "file_name": "pics.zip"})
        unnamed = await client.call_tool("store_file", {"file_content": b64(make_pdf())})
    zid, pid = _file_id_of(zipped.content[0].text), _file_id_of(unnamed.content[0].text)
    with rt.store.open(zid) as f:
        assert f.read() == archive.getvalue()
    assert rt.store.get(zid).kind == "upload" and rt.store.get(zid).name == "pics.zip"
    assert rt.store.get(pid).name == "file.pdf" and rt.store.get(pid).mime == "application/pdf"


async def test_store_file_refusals(tmp_path, monkeypatch):
    """TC-UPL-003: no file, a file name instead of content, and a file over the input limit
    are refused, and nothing is stored."""
    rt = _runtime(tmp_path)
    from src.domain.convert import service
    async with _client() as client:
        empty = await client.call_tool("store_file", {}, raise_on_error=False)
        named = await client.call_tool("store_file", {"file_content": "report.pdf"},
                                       raise_on_error=False)
        monkeypatch.setattr(service, "MAX_INPUT_MB", 0.0001)
        big = await client.call_tool("store_file", {"file_content": b64(make_pdf(5))},
                                     raise_on_error=False)
    assert empty.is_error and "No file provided" in empty.content[0].text
    assert named.is_error and "looks like a file name" in named.content[0].text
    assert big.is_error and "File too large" in big.content[0].text
    assert rt.store.stats()["files"] == 0


async def test_store_file_without_a_store():
    """TC-UPL-004: with no file store, store_file and create_upload say so (tool errors)."""
    async with _client() as client:
        a = await client.call_tool("store_file", {"file_content": b64(make_pdf())},
                                   raise_on_error=False)
        b = await client.call_tool("create_upload", {"file_name": "a.pdf"}, raise_on_error=False)
    assert a.is_error and b.is_error and "keeps no files" in a.content[0].text


async def test_store_file_counts_toward_the_owner_quota(tmp_path):
    """TC-UPL-005: store_file is held to the owner's share of the store."""
    rt = _runtime(tmp_path, max_owner_bytes=3000)
    rt.store.put_bytes(ANONYMOUS_OWNER, b"x" * 2500, name="old.bin", mime="application/octet-stream")
    async with _client() as client:
        result = await client.call_tool("store_file", {"file_content": b64(b"y" * 1000),
                                                       "file_name": "a.bin"},
                                        raise_on_error=False)
    assert result.is_error and "could not be kept" in result.content[0].text
    assert rt.store.stats()["files"] == 1


async def test_stored_file_belongs_to_its_caller(tmp_path, as_user):
    """TC-UPL-006: a file stored by alice cannot be used by bob, in files or as file_id."""
    _runtime(tmp_path)
    as_user("alice")
    async with _client() as client:
        stored = await client.call_tool("store_file", {"file_content": b64(make_pdf()),
                                                       "file_name": "a.pdf"})
        fid = _file_id_of(stored.content[0].text)
        as_user("bob")
        merge = await client.call_tool("pdf_merge", {"files": [
            {"file_id": fid}, {"file_content": b64(make_pdf())}]}, raise_on_error=False)
        as_user("alice")
        ok = await client.call_tool("pdf_merge", {"files": [
            {"file_id": fid}, {"file_content": b64(make_pdf())}]})
    assert merge.is_error and "made by someone else" in merge.content[0].text
    assert "File #1 in files" in merge.content[0].text
    assert not ok.is_error


# ---------------------------------------------------------------------------- files items

async def test_files_items_name_the_bad_one(tmp_path):
    """TC-UPL-007: an item with both sources, with none, with an unknown file_id or with
    unreadable content is refused, the error naming the item's position and name."""
    _runtime(tmp_path)
    pdf = b64(make_pdf())
    cases = {
        "both": [{"file_content": pdf}, {"file_content": pdf, "file_id": "f_" + "A" * 22,
                                         "file_name": "two.pdf"}],
        "none": [{"file_content": pdf}, {"file_name": "two.pdf"}],
        "unknown": [{"file_content": pdf}, {"file_id": "f_" + "A" * 22, "file_name": "two.pdf"}],
        "not a pdf": [{"file_content": pdf}, {"file_content": b64(b"plain words here"),
                                              "file_name": "two.txt"}],
    }
    async with _client() as client:
        for case, files in cases.items():
            result = await client.call_tool("pdf_merge", {"files": files}, raise_on_error=False)
            text = result.content[0].text
            # A file of the wrong format is the tool's refusal (its usual reply), not an error.
            assert result.is_error or case == "not a pdf", case
            assert "File #2" in text and "two." in text, (case, text)
    assert "gives both" in (await _call_text("pdf_merge", {"files": cases["both"]}))
    assert "gives no file" in (await _call_text("pdf_merge", {"files": cases["none"]}))


async def _call_text(tool, args):
    async with _client() as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return result.content[0].text


async def test_files_per_call_size_cap(tmp_path, monkeypatch):
    """TC-UPL-008: the files of one call may not add up to more than MAX_CALL_MB."""
    _runtime(tmp_path)
    monkeypatch.setattr(file_inputs, "MAX_CALL_MB", 0.0006)  # ~630 bytes
    pdf = make_pdf()
    assert len(pdf) * 2 > 0.0006 * MB > len(pdf)
    text = await _call_text("pdf_merge", {"files": [{"file_content": b64(pdf)}] * 2})
    assert "add up to more than" in text


async def test_merge_with_file_content_items_unchanged(tmp_path):
    """TC-UPL-009: blast radius: a 1.3-style pdf_merge call (base64 items with names) still
    merges exactly as before, also without a file store."""
    for rt in (None, "store"):
        if rt:
            _runtime(tmp_path, "inline")
        async with _client() as client:
            result = await client.call_tool("pdf_merge", {"files": [
                {"file_content": b64(make_pdf(1)), "file_name": "A.pdf"},
                {"file_content": b64(make_pdf(2)), "file_name": "B.pdf"}]})
        assert not result.is_error
        reader = PdfReader(io.BytesIO(base64.b64decode(result.content[1].resource.blob)))
        assert len(reader.pages) == 3 and [o.title for o in reader.outline] == ["A", "B"]


# ---------------------------------------------------------------------------- images_to_pdf

async def test_images_to_pdf_takes_several_files_in_order(tmp_path):
    """TC-UPL-010: images_to_pdf with files: one page per image, in the order given, mixing
    file_id and base64 items."""
    rt = _runtime(tmp_path)
    wide = rt.store.put_bytes(ANONYMOUS_OWNER, image((0, 0, 255), (60, 20)), name="wide.png",
                              mime="image/png")
    async with _client() as client:
        result = await client.call_tool("images_to_pdf", {"files": [
            {"file_content": b64(image((255, 0, 0), (20, 40))), "file_name": "tall.png"},
            {"file_id": wide.file_id},
            {"file_content": b64(image((0, 255, 0), (30, 30), "JPEG")), "file_name": "sq.jpg"}]})
    assert not result.is_error, result.content[0].text
    text = result.content[0].text
    assert "3-page PDF" in text and "from tall.png, wide.png, sq.jpg" in text
    reader = _merged_pages(result)
    sizes = [(round(float(p.mediabox.width)), round(float(p.mediabox.height)))
             for p in reader.pages]
    assert sizes == [(15, 30), (45, 15), (22, 22)]


async def test_images_to_pdf_files_refusals(tmp_path):
    """TC-UPL-011: files together with file_content, a PDF or a ZIP among the files, and a
    non-image item are refused, naming the item."""
    _runtime(tmp_path)
    img = b64(image((1, 1, 1)))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.png", image((1, 2, 3)))
    both = await _call_text("images_to_pdf", {"file_content": img, "files": [{"file_content": img}]})
    pdf = await _call_text("images_to_pdf", {"files": [
        {"file_content": img}, {"file_content": b64(make_pdf()), "file_name": "doc.pdf"}]})
    zipped = await _call_text("images_to_pdf", {"files": [
        {"file_content": img}, {"file_content": b64(archive.getvalue()), "file_name": "p.zip"}]})
    text = await _call_text("images_to_pdf", {"files": [
        {"file_content": img}, {"file_content": b64(b"hello"), "file_name": "n.txt"}]})
    assert "either in files or in file_content" in both
    assert "File #2 in files (doc.pdf) is a PDF" in pdf
    assert "'p.zip' is a ZIP" in zipped
    assert "'n.txt' is not an image" in text


async def test_images_to_pdf_single_file_unchanged(tmp_path):
    """TC-UPL-012: one image in file_content (no files) works as in 1.3, and an empty files
    list falls back to it."""
    _runtime(tmp_path, "inline")
    async with _client() as client:
        one = await client.call_tool("images_to_pdf", {"file_content": b64(image((9, 9, 9))),
                                                       "file_name": "a.png"})
        empty = await client.call_tool("images_to_pdf", {"file_content": b64(image((9, 9, 9))),
                                                         "files": None})
    for r in (one, empty):
        assert not r.is_error and r.content[0].text.startswith("Created a 1-page PDF")


# ---------------------------------------------------------------------------- uploads

async def _new_upload(file_name="scan.pdf", size_bytes=None):
    args = {"file_name": file_name} | ({"size_bytes": size_bytes} if size_bytes else {})
    async with _client() as client:
        result = await client.call_tool("create_upload", args, raise_on_error=False)
    return result


async def test_create_upload_put_then_merge(tmp_path):
    """TC-UPL-013: create_upload gives a URL and a file_id; a PUT of the raw bytes stores the
    file (201, its sha256); the file_id then merges like any other."""
    import hashlib
    rt = _runtime(tmp_path)
    made = await _new_upload("Scan 1.pdf")
    text = made.content[0].text
    url, fid = _url_from(text), _file_id_of(text)
    assert url.startswith(f"https://files.example/uploads/{fid}?exp=") and "curl -T" in text
    assert rt.store.get(fid) is None  # not usable before the upload
    data = make_pdf(2)
    response = _http().put(_path(url), content=data, headers={"content-type": "application/pdf"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["file_id"] == fid and body["size"] == len(data)
    assert body["sha256"] == hashlib.sha256(data).hexdigest() and body["name"] == "Scan 1.pdf"
    meta = rt.store.get(fid, ANONYMOUS_OWNER)
    assert meta.kind == "upload" and meta.mime == "application/pdf"
    async with _client() as client:
        merged = await client.call_tool("pdf_merge", {"files": [
            {"file_id": fid}, {"file_content": b64(make_pdf(1))}]})
    assert not merged.is_error and len(_merged_pages(merged).pages) == 3
    assert _merged_pages(merged).outline[0].title == "Scan 1"


async def test_upload_url_tampering_is_refused(tmp_path):
    """TC-UPL-014: a changed signature, expiry, size limit or id gets the same 404, and the
    real URL still works afterwards."""
    _runtime(tmp_path)
    url = _url_from((await _new_upload()).content[0].text)
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    fid = parts.path.rsplit("/", 1)[1]
    other = "f_" + "Z" * 22
    variants = [
        f"/uploads/{fid}?exp={q['exp']}&max={q['max']}&sig={q['sig'][:-2]}AA",
        f"/uploads/{fid}?exp={int(q['exp']) + 60}&max={q['max']}&sig={q['sig']}",
        f"/uploads/{fid}?exp={q['exp']}&max={int(q['max']) * 2}&sig={q['sig']}",
        f"/uploads/{other}?exp={q['exp']}&max={q['max']}&sig={q['sig']}",
        f"/uploads/{fid}?exp={q['exp']}&max={q['max']}",
        f"/uploads/..%2F..%2Fetc?exp={q['exp']}&max={q['max']}&sig={q['sig']}",
    ]
    http = _http()
    for path in variants:
        r = http.put(path, content=make_pdf())
        assert r.status_code == 404, (path, r.status_code)
        if "%2F" not in path:  # the last one never reaches the route
            assert r.json()["error"] == "not_found", path
    assert http.put(_path(url), content=make_pdf()).status_code == 201


async def test_upload_url_expires(tmp_path, monkeypatch):
    """TC-UPL-015: after UPLOAD_TTL_SEC the URL gets 404, even with a valid signature, and
    the sweep forgets it."""
    rt = _runtime(tmp_path)
    url = _url_from((await _new_upload()).content[0].text)
    later = time.time() + uploads_mod.UPLOAD_TTL_SEC + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert _http().put(_path(url), content=make_pdf()).status_code == 404
    assert rt.uploads.sweep() == 1 and rt.uploads.stats()["pending_uploads"] == 0


async def test_upload_url_is_single_use(tmp_path):
    """TC-UPL-016: a second PUT after a stored upload gets 409 already_used, and the file is
    not replaced; a PUT while one is in progress gets 409 upload_in_progress."""
    rt = _runtime(tmp_path)
    url = _url_from((await _new_upload()).content[0].text)
    http = _http()
    first = make_pdf(1)
    assert http.put(_path(url), content=first).status_code == 201
    again = http.put(_path(url), content=make_pdf(3))
    assert again.status_code == 409 and again.json()["error"] == "already_used"
    fid = urlsplit(url).path.rsplit("/", 1)[1]
    with rt.store.open(fid) as f:
        assert f.read() == first
    # in progress: claimed but not finished
    url2 = _url_from((await _new_upload()).content[0].text)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url2).query).items()}
    fid2 = urlsplit(url2).path.rsplit("/", 1)[1]
    pending = rt.uploads.claim(fid2, q["exp"], q["max"], q["sig"])
    busy = http.put(_path(url2), content=make_pdf())
    assert busy.status_code == 409 and busy.json()["error"] == "upload_in_progress"
    rt.uploads.finish(pending, stored=False)
    assert http.put(_path(url2), content=make_pdf()).status_code == 201


async def test_upload_size_cap(tmp_path):
    """TC-UPL-017: the URL's limit holds whatever the client says: a Content-Length over it,
    and a chunked body (no Content-Length) that grows over it, get 413 with nothing stored;
    the same URL then takes a file that fits."""
    rt = _runtime(tmp_path)
    url = _url_from((await _new_upload(size_bytes=1000)).content[0].text)
    assert "max=1000" in url
    http = _http()
    over = http.put(_path(url), content=b"x" * 1001)
    assert over.status_code == 413 and over.json()["max_bytes"] == 1000

    def chunks():
        for _ in range(10):
            yield b"y" * 300
    chunked = http.put(_path(url), content=chunks())
    assert chunked.status_code == 413
    assert rt.store.stats()["files"] == 0
    assert list(rt.uploads.parts_dir.iterdir()) == []
    assert rt.store._reserved_total == 0
    assert http.put(_path(url), content=b"z" * 1000).status_code == 201


async def _raw_put(url: str, chunks: list[bytes], content_length: str) -> int:
    """PUT through raw ASGI with the Content-Length a misbehaving client claims."""
    app = FastAPI()
    app.include_router(uploads_router)
    parts = urlsplit(url)
    body = list(chunks)
    sent = []

    async def receive():
        if body:
            return {"type": "http.request", "body": body.pop(0), "more_body": bool(body)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT",
             "scheme": "http", "path": parts.path, "raw_path": parts.path.encode(),
             "query_string": parts.query.encode(), "root_path": "",
             "headers": [(b"content-length", content_length.encode()), (b"host", b"test")],
             "client": ("127.0.0.1", 1), "server": ("test", 80)}
    await app(scope, receive, send)
    return sent[0]["status"]


async def test_upload_lying_content_length(tmp_path):
    """TC-UPL-018: a Content-Length that understates the body does not get the body past the
    limit (the bytes are counted as they arrive), and one over the limit is refused before
    the body is read."""
    rt = _runtime(tmp_path)
    url = _url_from((await _new_upload(size_bytes=1000)).content[0].text)
    assert await _raw_put(url, [b"a" * 600, b"b" * 600], "10") == 413
    assert await _raw_put(url, [b"c" * 10], "5000") == 413
    assert await _raw_put(url, [b"c" * 10], "ten") == 413
    assert rt.store.stats()["files"] == 0
    assert await _raw_put(url, [b"c" * 10], "10") == 201


async def test_upload_empty_body(tmp_path):
    """TC-UPL-019: an empty body gets 400, and the URL can still be used."""
    _runtime(tmp_path)
    url = _url_from((await _new_upload()).content[0].text)
    http = _http()
    assert http.put(_path(url), content=b"").status_code == 400
    assert http.put(_path(url), content=make_pdf()).status_code == 201


async def test_upload_counts_toward_the_owner_quota(tmp_path):
    """TC-UPL-020: an upload needs room for the URL's limit in its owner's share: without it
    507 storage_full before the body is read; the room is given back afterwards; another
    owner's files do not count."""
    rt = _runtime(tmp_path, max_owner_bytes=5000)
    rt.store.put_bytes(ANONYMOUS_OWNER, b"x" * 4500, name="old.bin", mime="application/octet-stream")
    rt.store.put_bytes("alice", b"x" * 4500, name="a.bin", mime="application/octet-stream")
    url = _url_from((await _new_upload(size_bytes=1000)).content[0].text)
    full = _http().put(_path(url), content=b"y" * 100)
    assert full.status_code == 507 and full.json()["error"] == "storage_full"
    assert rt.store._reserved_total == 0
    url2 = _url_from((await _new_upload(size_bytes=500)).content[0].text)
    assert _http().put(_path(url2), content=b"y" * 500).status_code == 201


async def test_uploaded_file_belongs_to_the_url_maker(tmp_path, as_user):
    """TC-UPL-021: the uploaded file is alice's (who made the URL), whoever sends the bytes;
    bob cannot use its file_id."""
    rt = _runtime(tmp_path)
    as_user("alice")
    text = (await _new_upload()).content[0].text
    fid = _file_id_of(text)
    assert _http().put(_path(_url_from(text)), content=make_pdf()).status_code == 201
    assert rt.store.get(fid, "alice") is not None and rt.store.get(fid, "bob") is None
    as_user("bob")
    refused = await _call_text("pdf_merge", {"files": [{"file_id": fid},
                                                      {"file_content": b64(make_pdf())}]})
    assert "made by someone else" in refused


async def test_create_upload_size_limits(tmp_path, monkeypatch):
    """TC-UPL-022: size_bytes over the input limit is refused; without it the URL takes up
    to MAX_INPUT_MB."""
    _runtime(tmp_path)
    from src.domain.convert import service
    monkeypatch.setattr(service, "MAX_INPUT_MB", 1)
    big = await _new_upload(size_bytes=MB + 1)
    assert big.is_error and "larger than the 1 MB" in big.content[0].text
    default = await _new_upload()
    assert f"max={MB}&" in _url_from(default.content[0].text)


def test_parts_dir_is_emptied_at_start_and_on_failure(tmp_path):
    """TC-UPL-023: leftovers of an upload cut off by a crash are deleted at startup."""
    parts = tmp_path / "uploads"
    parts.mkdir()
    (parts / "f_xxxxxxxxxxxxxxxxxxxxxx.part").write_bytes(b"half")
    Uploads(parts, LinkSigner(KEY, "https://h"))
    assert list(parts.iterdir()) == []


def test_store_put_file_with_a_given_id_and_reservation(tmp_path):
    """TC-UPL-024: put_file under a given id refuses an id in use or malformed; with the room
    already reserved it does not claim it twice (a file exactly at the owner cap fits)."""
    store = LocalStore(tmp_path / "s", max_owner_bytes=1000)
    src = tmp_path / "blob"
    src.write_bytes(b"q" * 1000)
    fid = "f_" + "C" * 22
    with store.reserve("o", 1000):
        meta = store.put_file("o", src, name="a", mime="x/y", file_id=fid, reserved_bytes=1000)
    assert meta.file_id == fid and store._reserved_total == 0
    with pytest.raises(FileExistsError):
        store.put_file("p", src, name="a", mime="x/y", file_id=fid)
    with pytest.raises(ValueError):
        store.put_file("p", src, name="a", mime="x/y", file_id="../x")
    with pytest.raises(StoreFullError):
        with store.reserve("o", 1):
            pass


def test_upload_signature_is_not_a_download_signature():
    """TC-UPL-025: a download signature never passes as an upload one (and the reverse)."""
    signer = LinkSigner(KEY, "https://h")
    fid, exp = "f_" + "D" * 22, int(time.time()) + 60
    assert signer.verify_upload(fid, str(exp), "100", signer.upload_signature(fid, exp, 100))
    assert not signer.verify_upload(fid, str(exp), "100", signer.signature(fid, exp, "100"))
    assert not signer.verify(fid, "100", str(exp), signer.upload_signature(fid, exp, 100))
    assert not signer.verify_upload(fid, "-1", "100", "x")


def test_access_log_drops_upload_signature():
    """TC-UPL-026: uvicorn's access line for /uploads/ carries no query string."""
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "PUT", "/uploads/f_x?exp=1&max=2&sig=SECRET", "1.1", 201),
                            None)
    _RedactFileQueries().filter(rec)
    assert "SECRET" not in rec.getMessage() and "/uploads/f_x?…" in rec.getMessage()


async def test_files_lifespan_sweeps_uploads(tmp_path, monkeypatch):
    """TC-UPL-027: the store's sweep also forgets expired upload URLs."""
    from src.domain import files
    rt = _runtime(tmp_path)
    rt.uploads.create("o", "a.pdf", 10, now=time.time() - 2 * uploads_mod.UPLOAD_TTL_SEC)
    assert rt.uploads.stats()["pending_uploads"] == 1
    async with files.files_lifespan():
        await asyncio.sleep(0)
    assert rt.uploads.stats()["pending_uploads"] == 0

