"""The operations registry (src/domain/operations/) behind the MCP tools."""

import asyncio
import io
import json

import pytest
from fastmcp import Client, FastMCP
from PIL import Image
from pydantic import BaseModel, ValidationError
from pypdf import PdfReader, PdfWriter

from src.domain.convert.pdf_pages import InvalidPdfOptionError
from src.domain.operations import (
    Arity,
    Artifact,
    Operation,
    OperationRefused,
    OpResult,
    execute,
    get_operation,
    list_operations,
)
from src.domain.operations import base as ops_base
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

# Tool name -> the parameters that carry the input file(s), not the operation's arguments
INPUT_PARAMS = {"file_content", "file_name", "mime_type", "file_id", "files"}

EXPECTED_ARITY = {
    "list_supported_conversions": Arity.NONE_TO_REPORT,
    "inspect_document": Arity.ONE_TO_REPORT,
    "convert_document": Arity.ONE_TO_ONE,
    "extract_text": Arity.ONE_TO_REPORT,
    "pdf_extract_pages": Arity.ONE_TO_ONE,
    "pdf_split": Arity.ONE_TO_MANY,
    "pdf_rotate": Arity.ONE_TO_ONE,
    "pdf_compress": Arity.ONE_TO_ONE,
    "pdf_protect": Arity.ONE_TO_ONE,
    "pdf_unlock": Arity.ONE_TO_ONE,
    "pdf_metadata": Arity.ONE_TO_REPORT,
    "pdf_strip_metadata": Arity.ONE_TO_ONE,
    "pdf_merge": Arity.MANY_TO_ONE,
    "pdf_delete_pages": Arity.ONE_TO_ONE,
    "pdf_reorder_pages": Arity.ONE_TO_ONE,
    "pdf_watermark": Arity.ONE_TO_ONE,
    "pdf_page_numbers": Arity.ONE_TO_ONE,
    "pdf_to_images": Arity.ONE_TO_ONE,
    "images_to_pdf": Arity.ONE_TO_ONE,
}


def make_pdf(n=3) -> bytes:
    w = PdfWriter()
    for i in range(n):
        w.add_blank_page(100 + i, 400)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def source(data: bytes, fmt: str | None, name="doc", file_name=None, calls=None):
    def resolve():
        if calls is not None:
            calls.append(name)
        return Artifact(data=data, fmt=fmt, name=name, file_name=file_name)
    return resolve


async def _tools() -> dict[str, dict]:
    mcp = FastMCP("ops-test")
    register_morphyard_tools(mcp)
    async with Client(mcp) as client:
        return {t.name: t.inputSchema for t in await client.list_tools()
                if t.name not in JOB_TOOLS}


# The job tools reach jobs, not operations (issue #34).
JOB_TOOLS = {"job_status", "job_cancel", "job_list"}


def test_every_tool_has_exactly_one_operation():
    """TC-OPS-002: the registry holds one operation per MCP tool (the job tools aside),
    and nothing else."""
    tools = asyncio.run(_tools())
    assert {op.name for op in list_operations()} == set(tools)
    assert len(list_operations()) == 19
    for name in tools:
        assert get_operation(name).name == name
    with pytest.raises(KeyError, match="unknown operation 'pdf_teleport'"):
        get_operation("pdf_teleport")


def test_tool_arguments_come_from_the_operation_model():
    """TC-OPS-003: a tool's parameters are its operation's arguments (same names,
    required set and defaults) plus its input parameters, nothing more."""
    tools = asyncio.run(_tools())
    for name, schema in tools.items():
        model = get_operation(name).args_model.model_json_schema()
        props = {k: v for k, v in schema["properties"].items() if k not in INPUT_PARAMS}
        assert set(props) == set(model.get("properties", {})), name
        assert set(schema.get("required", [])) - INPUT_PARAMS == set(model.get("required", [])), name
        for field, spec in props.items():
            want = model["properties"][field]
            assert spec.get("default") == want.get("default"), (name, field)
            assert spec.get("description") == want.get("description"), (name, field)


def test_arity_and_formats_are_declared():
    """TC-OPS-004: arity per operation; PDF operations accept only PDF."""
    for op in list_operations():
        assert op.arity is EXPECTED_ARITY[op.name], op.name
        assert op.arity.produces_files == (op.name not in {
            "list_supported_conversions", "inspect_document", "extract_text", "pdf_metadata"})
        json.dumps(op.describe())  # JSON-ready for listings and the pipeline
        if op.name.startswith("pdf_"):
            assert op.input_formats == frozenset({"pdf"}), op.name
    assert get_operation("images_to_pdf").detect_input_format is False
    assert get_operation("images_to_pdf").output_formats == frozenset({"pdf"})
    assert get_operation("pdf_to_images").output_formats == frozenset({"png", "jpg", "webp", "zip"})
    assert get_operation("convert_document").output_formats is None


def test_execute_checks_the_number_of_inputs():
    """TC-OPS-005: an operation given the wrong number of inputs is a programming error."""
    pdf = source(make_pdf(), "pdf")
    with pytest.raises(ValueError, match="pdf_rotate.*given 2 input"):
        asyncio.run(execute("pdf_rotate", {"degrees": 90}, [pdf, pdf]))
    with pytest.raises(ValueError, match="given 0 input"):
        asyncio.run(execute("pdf_merge", {}, []))
    with pytest.raises(ValueError, match="given 1 input"):
        asyncio.run(execute("list_supported_conversions", {}, [pdf]))


def test_execute_runs_an_operation_without_mcp():
    """TC-OPS-006: execute() is a complete path of its own (jobs and the pipeline use it):
    argument defaults apply and the result carries the files."""
    result = asyncio.run(execute("pdf_rotate", {"degrees": 90},
                                 [source(make_pdf(), "pdf", name="scan")]))
    assert isinstance(result, OpResult)
    assert result.summary == "Rotated all pages by 90 degrees"
    [out] = result.files
    assert (out.fmt, out.name) == ("pdf", "scan_rotated")
    assert [p.get("/Rotate") for p in PdfReader(io.BytesIO(out.data)).pages] == [90] * 3

    split = asyncio.run(execute("pdf_split", {}, [source(make_pdf(3), "pdf")]))
    assert [f.name for f in split.files] == ["doc_p1", "doc_p2", "doc_p3"]

    merged = asyncio.run(execute("pdf_merge", {"bookmarks": False}, [
        source(make_pdf(2), "pdf", name="a", file_name="a.pdf"),
        source(make_pdf(1), "pdf", name="b", file_name="b.pdf")]))
    assert merged.files[0].name == "a_merged"
    assert len(PdfReader(io.BytesIO(merged.files[0].data)).pages) == 3


def test_arguments_are_validated_by_the_model():
    """TC-OPS-007: unknown or mistyped arguments are refused before any work."""
    op = get_operation("pdf_compress")
    with pytest.raises(ValidationError):
        op.parse_args({"level": "small", "colour": "red"})
    with pytest.raises(ValidationError):
        op.parse_args({"max_dpi": "lots"})
    with pytest.raises(ValidationError):
        get_operation("pdf_unlock").parse_args({})
    assert op.parse_args({}).level == "balanced"


def test_argument_checks_run_before_any_input_is_read():
    """TC-OPS-008: prepare() runs before the inputs are resolved, and inputs are read
    one at a time in order; a refused input stops the next one from being read."""
    calls: list[str] = []
    with pytest.raises(InvalidPdfOptionError, match="pages is empty"):
        asyncio.run(execute("pdf_delete_pages", {"pages": " "},
                            [source(make_pdf(), "pdf", calls=calls)]))
    with pytest.raises(InvalidPdfOptionError, match="unknown colour"):
        asyncio.run(execute("pdf_watermark", {"text": "A", "color": "blurple"},
                            [source(make_pdf(), "pdf", calls=calls)]))
    assert calls == []

    with pytest.raises(OperationRefused, match=r"File #1 \(p.png\) is not a PDF"):
        asyncio.run(execute("pdf_merge", {}, [
            source(png(), "png", name="p", file_name="p.png", calls=calls),
            source(make_pdf(), "pdf", name="second", calls=calls)]))
    assert calls == ["p"]

    # pdf_to_images has always refused a non-PDF before looking at its options
    with pytest.raises(OperationRefused, match="not a PDF"):
        asyncio.run(execute("pdf_to_images", {"dpi": 9999}, [source(png(), "png")]))


def test_progress_messages_go_through_notify():
    """TC-OPS-009: convert_document's start message comes first, before the target is
    checked (as the tool's ctx.info always did)."""
    said: list[str] = []

    async def notify(message):
        said.append(message)
    with pytest.raises(Exception, match="not a target format"):
        asyncio.run(execute("convert_document", {"target_format": "wav", "output_name": "x"},
                            [source(b"# a", "md")], notify=notify))
    assert said == ["Converting to wav..."]


async def test_refusals_keep_each_tool_reply_shape():
    """TC-OPS-010: a refused input answers in the tool's own shape: a list for most
    tools, plain text for the pdf_secure ones, {"error": ...} for pdf_metadata."""
    import base64
    mcp = FastMCP("ops-test")
    register_morphyard_tools(mcp)
    b64 = base64.b64encode(png()).decode()
    async with Client(mcp) as client:
        listed = await client.call_tool("pdf_rotate", {"degrees": 90, "file_content": b64})
        text = await client.call_tool("pdf_compress", {"file_content": b64})
        report = await client.call_tool("pdf_metadata", {"file_content": b64})
        unlocked = await client.call_tool("pdf_unlock", {
            "password": "x", "file_content": base64.b64encode(make_pdf()).decode()})
    assert listed.content[0].text == '["This is not a PDF (detected png)."]'
    assert text.content[0].text == ("This is not a PDF (detected png). Convert it to PDF "
                                    "with convert_document first.")
    assert report.structured_content == {"error": text.content[0].text}
    assert [c.type for c in unlocked.content] == ["text"]
    assert unlocked.content[0].text == "This PDF is not encrypted; there is nothing to unlock."


def test_registry_refuses_inconsistent_operations(monkeypatch):
    """TC-OPS-011: an operation name registers once, and a format restriction needs a
    refusal message."""
    monkeypatch.setattr(ops_base, "_REGISTRY", dict(ops_base._REGISTRY))

    class NoArgs(BaseModel):
        pass

    async def run(call):
        return OpResult()
    with pytest.raises(ValueError, match="registered twice"):
        ops_base.register(Operation("pdf_rotate", "x", NoArgs, Arity.ONE_TO_ONE, run))
    with pytest.raises(ValueError, match="without a message"):
        ops_base.register(Operation("new_op", "x", NoArgs, Arity.ONE_TO_ONE, run,
                                    input_formats=frozenset({"pdf"})))


async def test_the_callers_file_name_reaches_the_operation():
    """TC-OPS-012: the name exactly as the caller gave it reaches the operation: it is
    what inspect_document reports and what pdf_merge titles each bookmark with."""
    import base64
    mcp = FastMCP("ops-test")
    register_morphyard_tools(mcp)
    b64 = base64.b64encode(make_pdf()).decode()
    async with Client(mcp) as client:
        info = await client.call_tool("inspect_document", {"file_content": b64,
                                                           "file_name": "Q3 Report.pdf"})
        merged = await client.call_tool("pdf_merge", {"files": [
            {"file_content": b64, "file_name": "Part one.pdf"},
            {"file_content": b64, "file_name": "Part two.pdf"}]})
    assert info.structured_content["file_name"] == "Q3 Report.pdf"
    pdf = PdfReader(io.BytesIO(base64.b64decode(merged.content[1].resource.blob)))
    assert [item.title for item in pdf.outline] == ["Part one", "Part two"]
    from urllib.parse import unquote
    assert unquote(str(merged.content[1].resource.uri)).endswith("/Part one_merged.pdf")
