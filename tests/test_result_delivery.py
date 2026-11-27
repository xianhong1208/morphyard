"""Results kept on the server and returned as links; file_id inputs (issue #33, PR 3).

The reply shape per MORPHYARD_RESULT_DELIVERY, the file_id input on every tool that takes a
file, the owner check, and the all-or-nothing store write when the store is full.
"""

import base64
import dataclasses
import io
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from mcp_types import ResourceLink
from PIL import Image
from pypdf import PdfReader, PdfWriter
from starlette.testclient import TestClient

from src.api.router.files import router as files_router
from src.domain.files import (
    FilesRuntime,
    LinkSigner,
    LocalStore,
    NewFile,
    StoreFullError,
    keep_results,
    owner_key,
    set_runtime,
)
from src.domain.operations.sources import ANONYMOUS_OWNER, BOTH_GIVEN_MESSAGE
from src.fastmcp_tools import operation_tool
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

KEY = b"d" * 32
MB = 1024 * 1024


def make_pdf(n=3, width=200) -> bytes:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width, 300)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 20), (0, 0, 255)).save(out, format="PNG")
    return out.getvalue()


def _runtime(tmp_path, mode="auto", inline_max_bytes=10 * MB, **store_kw):
    store = LocalStore(tmp_path / "store", **store_kw)
    rt = FilesRuntime(store=store, signer=LinkSigner(KEY, "https://files.example"),
                      sweep_interval_sec=60, result_delivery=mode,
                      inline_max_bytes=inline_max_bytes)
    set_runtime(rt)
    return rt


def _client():
    mcp = FastMCP("delivery-test")
    register_morphyard_tools(mcp)
    return Client(mcp)


def _download(url: str) -> bytes:
    app = FastAPI()
    app.include_router(files_router)
    parts = urlsplit(url)
    response = TestClient(app).get(parts.path + "?" + parts.query)
    assert response.status_code == 200, response.text
    return response.content


def _kinds(result):
    return [type(block).__name__ for block in result.content]


def _links(result) -> list[ResourceLink]:
    return [b for b in result.content if isinstance(b, ResourceLink)]


def _file_ids(result) -> list[str]:
    return [link.description.rsplit("file_id ", 1)[1] for link in _links(result)]


ROTATE = {"degrees": 90}


# ---------------------------------------------------------------------- reply shape per mode

async def test_inline_without_a_store_is_the_1_3_reply():
    """TC-DLV-001: without a file store the reply is [summary, file] exactly as in 1.3."""
    async with _client() as client:
        result = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                       "file_name": "a.pdf"})
    assert _kinds(result) == ["TextContent", "EmbeddedResource"]
    assert "http" not in result.content[0].text and "file_id" not in result.content[0].text


async def test_inline_mode_stores_nothing(tmp_path):
    """TC-DLV-002: `inline` keeps the 1.3 reply and puts nothing in the store."""
    rt = _runtime(tmp_path, "inline")
    async with _client() as client:
        result = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                       "file_name": "a.pdf"})
    assert _kinds(result) == ["TextContent", "EmbeddedResource"]
    assert rt.store.stats()["files"] == 0


async def test_auto_small_result_is_inline_and_linked(tmp_path):
    """TC-DLV-003: `auto` with a small result: the 1.3 text and file blocks (the file block
    byte for byte), then a resource_link; the text names URL, name, size, expiry and file_id,
    and the link downloads the same bytes."""
    async with _client() as client:
        args = {**ROTATE, "file_content": b64(make_pdf()), "file_name": "a.pdf"}
        before = await client.call_tool("pdf_rotate", args)  # no store: the 1.3 reply
        _runtime(tmp_path, "auto")
        result = await client.call_tool("pdf_rotate", args)
    assert _kinds(result) == ["TextContent", "EmbeddedResource", "ResourceLink"]
    embedded, link = result.content[1], result.content[2]
    old = before.content[1]
    assert embedded.model_dump() == old.model_dump() | {
        "resource": old.resource.model_dump() | {"blob": embedded.resource.blob}}
    data = base64.b64decode(embedded.resource.blob)
    assert embedded.resource.uri == old.resource.uri
    assert embedded.resource.mime_type == old.resource.mime_type == "application/pdf"
    text = result.content[0].text
    assert text.startswith(before.content[0].text + "\n\n")
    file_id = _file_ids(result)[0]
    assert str(link.uri) in text and f"file_id {file_id}" in text and "a_rotated.pdf (" in text
    assert "valid until" in text and " UTC" in text and "link only" not in text
    assert link.name == "a_rotated.pdf" and link.mime_type == "application/pdf" and link.size == len(data)
    assert _download(str(link.uri)) == data


async def test_auto_large_result_is_a_link_only(tmp_path):
    """TC-DLV-004: `auto` with a result over the inline limit: text + resource_link only, and
    the text says the file is not attached."""
    _runtime(tmp_path, "auto", inline_max_bytes=100)
    async with _client() as client:
        result = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                       "file_name": "a.pdf"})
    assert _kinds(result) == ["TextContent", "ResourceLink"]
    text = result.content[0].text
    assert ", link only): https://" in text and "not attached" in text and "give the user the link" in text
    assert _download(str(result.content[1].uri))[:5] == b"%PDF-"


async def test_auto_limit_is_per_file(tmp_path):
    """TC-DLV-005: under `auto` each file of a result is inline or not by its own size (the
    limit itself is inline); files first, then one link per file, in order."""
    from src.domain.operations import OpResult, OutputFile
    _runtime(tmp_path, "auto", inline_max_bytes=500)
    result = OpResult(summary="Split", files=[OutputFile(b"%PDF-" + b"a" * 1000, "pdf", "big"),
                                              OutputFile(b"%PDF-" + b"b" * 495, "pdf", "edge"),
                                              OutputFile(b"%PDF-" + b"c" * 10, "pdf", "small")])
    reply = await operation_tool.deliver_files(result, ANONYMOUS_OWNER)
    assert [type(x).__name__ for x in reply] == ["str", "TypedFile", "TypedFile",
                                                 "ResourceLink", "ResourceLink", "ResourceLink"]
    assert [x._name for x in reply[1:3]] == ["edge", "small"]
    assert [x.name for x in reply[3:]] == ["big.pdf", "edge.pdf", "small.pdf"]
    assert reply[0].count(", link only): ") == 1 and "big.pdf (1005 bytes, file_id f_" in reply[0]


async def test_both_mode_always_inlines(tmp_path):
    """TC-DLV-006: `both` returns the file inline even over the limit, plus the link."""
    _runtime(tmp_path, "both", inline_max_bytes=1)
    async with _client() as client:
        result = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                       "file_name": "a.pdf"})
    assert _kinds(result) == ["TextContent", "EmbeddedResource", "ResourceLink"]
    assert "link only" not in result.content[0].text


async def test_link_mode_never_inlines(tmp_path):
    """TC-DLV-007: `link` never returns the file inline, however small."""
    _runtime(tmp_path, "link", inline_max_bytes=100 * MB)
    async with _client() as client:
        result = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                       "file_name": "a.pdf"})
    assert _kinds(result) == ["TextContent", "ResourceLink"]
    assert "is not attached" in result.content[0].text


async def test_text_only_answers_and_reports_are_unchanged(tmp_path):
    """TC-DLV-008: an answer without a file (pdf_unlock on an unencrypted PDF) stays plain text,
    and report tools reply as before; nothing is stored."""
    rt = _runtime(tmp_path, "auto")
    async with _client() as client:
        unlock = await client.call_tool("pdf_unlock", {"password": "x", "file_content": b64(make_pdf()),
                                                       "file_name": "a.pdf"})
        meta = await client.call_tool("pdf_metadata", {"file_content": b64(make_pdf()), "file_name": "a.pdf"})
    assert _kinds(unlock) == ["TextContent"] and "http" not in unlock.content[0].text
    assert meta.structured_content["page_count"] == 3
    assert rt.store.stats()["files"] == 0


# ---------------------------------------------------------------------- store full

async def test_store_full_is_a_clear_error_and_stores_nothing(tmp_path):
    """TC-DLV-010: a result that does not fit is a tool error naming the limit and when to
    retry; a split whose first file fits but not the rest leaves nothing in the store."""
    pdf = make_pdf(3)
    async with _client() as client:
        probe = await client.call_tool("pdf_split", {"pages_per_file": 1, "file_content": b64(pdf),
                                                     "file_name": "s.pdf"})
        first = len(base64.b64decode(probe.content[1].resource.blob))
        rt = _runtime(tmp_path, "auto", max_total_bytes=first + 10, max_owner_bytes=first + 10)
        result = await client.call_tool("pdf_split", {"pages_per_file": 1, "file_content": b64(pdf),
                                                      "file_name": "s.pdf"}, raise_on_error=False)
    assert result.is_error
    text = result.content[0].text
    assert "could not be kept" in text and "MB" in text and "Retry after about" in text
    assert "Nothing was stored" in text
    assert rt.store.stats()["files"] == 0
    assert not any(rt.store.root.glob("*/f_*"))


async def test_result_that_can_never_fit(tmp_path):
    """TC-DLV-012: a result larger than the per-person cap says retrying will not help (no
    retry time), and nothing is stored."""
    rt = _runtime(tmp_path, "auto", max_owner_bytes=100, max_total_bytes=10 * MB)
    async with _client() as client:
        result = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                       "file_name": "a.pdf"}, raise_on_error=False)
    text = result.content[0].text
    assert result.is_error and "can never be stored" in text and "retrying will not succeed" in text
    assert "Retry after" not in text and rt.store.stats()["files"] == 0


def test_keep_results_is_all_or_nothing(tmp_path):
    """TC-DLV-011: keep_results deletes what it stored when a later file does not fit."""
    store = LocalStore(tmp_path / "s", max_total_bytes=150, max_owner_bytes=150)
    signer = LinkSigner(KEY, "https://x")
    with pytest.raises(StoreFullError):
        keep_results(store, signer, "alice", [NewFile(b"a" * 100, "a.pdf", "application/pdf"),
                                              NewFile(b"b" * 100, "b.pdf", "application/pdf")])
    assert store.stats()["files"] == 0
    kept = keep_results(store, signer, "alice", [NewFile(b"a" * 100, "a.pdf", "application/pdf")])
    assert kept[0].meta.owner_key == owner_key("alice") and "/files/" + kept[0].meta.file_id in kept[0].url


# ---------------------------------------------------------------------- file_id inputs

async def test_chain_by_file_id(tmp_path):
    """TC-DLV-020: rotate -> watermark by file_id -> compress by file_id; each step works on
    the stored result, and the stored name carries through."""
    rt = _runtime(tmp_path, "link")
    async with _client() as client:
        r1 = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                   "file_name": "report.pdf"})
        r2 = await client.call_tool("pdf_watermark", {"text": "DRAFT", "file_id": _file_ids(r1)[0]})
        r3 = await client.call_tool("pdf_compress", {"file_id": _file_ids(r2)[0]})
    assert not r2.is_error and not r3.is_error
    assert _links(r3)[0].name.startswith("report")
    final = _download(str(_links(r3)[0].uri))
    reader = PdfReader(io.BytesIO(final))
    assert len(reader.pages) == 3 and reader.pages[0].rotation == 90
    assert rt.store.stats()["files"] == 3


async def test_report_tools_and_images_to_pdf_take_a_file_id(tmp_path):
    """TC-DLV-021: inspect_document / extract_text-style report tools and images_to_pdf (raw
    input, no format detection) accept a file_id too."""
    rt = _runtime(tmp_path, "auto")
    pdf = rt.store.put_bytes(ANONYMOUS_OWNER, make_pdf(2), name="two.pdf", mime="application/pdf")
    img = rt.store.put_bytes(ANONYMOUS_OWNER, png(), name="pic.png", mime="image/png")
    async with _client() as client:
        info = await client.call_tool("inspect_document", {"file_id": pdf.file_id})
        made = await client.call_tool("images_to_pdf", {"file_id": img.file_id})
    assert info.structured_content["detected_format"] == "pdf"
    assert info.structured_content["file_name"] == "two.pdf"
    assert info.structured_content["page_count"] == 2
    assert not made.is_error and _links(made)[0].name == "pic.pdf"


async def test_merge_items_take_a_file_id(tmp_path):
    """TC-DLV-022: pdf_merge items mix file_content and file_id; the stored name is the
    bookmark title."""
    rt = _runtime(tmp_path, "auto")
    stored = rt.store.put_bytes(ANONYMOUS_OWNER, make_pdf(2), name="Chapter 2.pdf", mime="application/pdf")
    async with _client() as client:
        result = await client.call_tool("pdf_merge", {"files": [
            {"file_content": b64(make_pdf(1)), "file_name": "Chapter 1.pdf"},
            {"file_id": stored.file_id}]})
    assert not result.is_error
    reader = PdfReader(io.BytesIO(base64.b64decode(result.content[1].resource.blob)))
    assert len(reader.pages) == 3
    assert [o.title for o in reader.outline] == ["Chapter 1", "Chapter 2"]


@pytest.mark.parametrize("case", ["unknown", "expired", "foreign", "malformed"])
async def test_unusable_file_id_is_refused(tmp_path, case):
    """TC-DLV-023: an unknown, expired or someone else's file_id gets one and the same
    refusal (no oracle); a malformed id is refused too."""
    rt = _runtime(tmp_path, "auto")
    if case == "unknown":
        file_id = "f_" + "A" * 22
    elif case == "foreign":
        file_id = rt.store.put_bytes("alice", make_pdf(), name="a.pdf", mime="application/pdf").file_id
    elif case == "expired":
        meta = rt.store.put_bytes(ANONYMOUS_OWNER, make_pdf(), name="a.pdf", mime="application/pdf")
        rt.store._index[meta.file_id] = dataclasses.replace(meta, expires_at=meta.created_at - 1)
        file_id = meta.file_id
    else:
        file_id = "report.pdf"
    async with _client() as client:
        result = await client.call_tool("pdf_rotate", {**ROTATE, "file_id": file_id},
                                        raise_on_error=False)
        merge = await client.call_tool("pdf_merge", {"files": [
            {"file_content": b64(make_pdf(1))}, {"file_id": file_id}]}, raise_on_error=False)
    for r in (result, merge):
        assert r.is_error
        text = r.content[0].text
        assert "is unknown, has expired, or was made by someone else" in text
        if case != "malformed":
            assert file_id in text


async def test_file_id_with_auth_belongs_to_the_token_sub(tmp_path, monkeypatch):
    """TC-DLV-024: with a token the result is stored for its `sub`; another `sub` cannot use
    the file_id, the same `sub` can."""

    class Token:
        def __init__(self, sub, client_id="c1"):
            self.claims = {"sub": sub} if sub else {}
            self.client_id = client_id

    rt = _runtime(tmp_path, "link")
    who = {"token": Token("alice")}
    monkeypatch.setattr(operation_tool, "get_access_token", lambda: who["token"])
    async with _client() as client:
        made = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                     "file_name": "a.pdf"})
        file_id = _file_ids(made)[0]
        assert rt.store.get(file_id, "alice") is not None
        who["token"] = Token("bob")
        refused = await client.call_tool("pdf_rotate", {**ROTATE, "file_id": file_id}, raise_on_error=False)
        who["token"] = Token("alice")
        again = await client.call_tool("pdf_rotate", {**ROTATE, "file_id": file_id})
    assert refused.is_error and "made by someone else" in refused.content[0].text
    assert not again.is_error


def test_owner_of_a_call(monkeypatch):
    """TC-DLV-025: owner = the token's sub; a token without sub -> its client id; no token
    (auth off) -> "anonymous"."""

    class Token:
        def __init__(self, claims, client_id="svc"):
            self.claims, self.client_id = claims, client_id

    for token, want in ((Token({"sub": "u1"}), "u1"), (Token({}), "client:svc"),
                        (Token({"sub": ""}), "client:svc"), (None, "anonymous")):
        monkeypatch.setattr(operation_tool, "get_access_token", lambda t=token: t)
        assert operation_tool.current_owner() == want


async def test_both_inputs_or_neither(tmp_path):
    """TC-DLV-026: file_content and file_id together are refused; an empty file_id (a host
    blanks the parameter it took an upload reference from) falls back to file_content;
    neither is the "no file" answer that mentions file_id."""
    rt = _runtime(tmp_path, "auto")
    stored = rt.store.put_bytes(ANONYMOUS_OWNER, make_pdf(), name="a.pdf", mime="application/pdf")
    async with _client() as client:
        both = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                     "file_id": stored.file_id}, raise_on_error=False)
        blank = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": b64(make_pdf()),
                                                      "file_name": "a.pdf", "file_id": ""})
        neither = await client.call_tool("pdf_rotate", {**ROTATE}, raise_on_error=False)
    assert both.is_error and both.content[0].text.endswith(BOTH_GIVEN_MESSAGE)
    assert not blank.is_error
    assert neither.is_error and "pass its file_id" in neither.content[0].text


async def test_bad_options_are_still_answered_before_the_file_is_read(tmp_path):
    """TC-DLV-027: an argument error wins over an unusable file_id (inputs stay lazy)."""
    _runtime(tmp_path, "auto")
    async with _client() as client:
        result = await client.call_tool("pdf_watermark", {"file_id": "f_" + "A" * 22},
                                        raise_on_error=False)
    text = result.content[0].text
    assert "made by someone else" not in text and "text" in text


def test_links_text_wording():
    """TC-DLV-028: one file vs several, auto vs link wording."""
    from src.domain.files import KeptFile, StoredFile
    meta = StoredFile(file_id="f_" + "B" * 22, owner_key="0" * 32, name="x.pdf",
                      mime="application/pdf", size=2 * MB, sha256="", created_at=0, expires_at=3600)
    k = KeptFile(meta=meta, url="https://h/files/x")
    one = operation_tool.links_text([k], [False], "link", 10 * MB, now=0)
    assert "Download link (valid until 1970-01-01 01:00 UTC, about 60 min)" in one
    assert "x.pdf (2.0 MB, file_id f_BBBB" in one and "The file is not attached" in one
    two = operation_tool.links_text([k, k], [True, False], "auto", 10 * MB, now=0)
    assert "Download links" in two and "larger than 10 MB" in two
    assert "To use a result in another Morphyard tool, pass its file_id" in two


@pytest.mark.parametrize("tool", ["pdf_rotate", "images_to_pdf"])
async def test_stored_input_keeps_the_size_limit(tmp_path, monkeypatch, tool):
    """TC-DLV-029: a file_id input is held to the same input size limit as an upload, with
    and without format detection."""
    from src.domain.convert import service
    monkeypatch.setattr(service, "MAX_INPUT_MB", 0.00001)
    rt = _runtime(tmp_path, "auto")
    data, name = (make_pdf(), "a.pdf") if tool == "pdf_rotate" else (png(), "a.png")
    stored = rt.store.put_bytes(ANONYMOUS_OWNER, data, name=name, mime="application/octet-stream")
    args = {"file_id": stored.file_id} | (ROTATE if tool == "pdf_rotate" else {})
    async with _client() as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.is_error and "File too large" in result.content[0].text
