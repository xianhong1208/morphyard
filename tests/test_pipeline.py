"""The pipeline tool (issue #37): several tools in a row on the server, as one job.

Steps are checked before any input is read and every refusal names the step; files flow by
each operation's arity (one-to-one maps over every file, a split fans out, a merge combines);
only the last step's files come back, as links or one ZIP; a step failing mid-way names the
step and keeps nothing; the whole pipeline is one job (job_id after wait_sec, progress as
steps done of steps, job_cancel kills the running engine); inputs and results follow the
owner rules of every other tool.
"""

import asyncio
import base64
import io
import os
import re
import time
import zipfile

import pytest
from fastmcp import Client, FastMCP
from mcp_types import ResourceLink
from pypdf import PdfReader, PdfWriter

from src.domain import jobs as jobs_mod
from src.domain.convert import limiter as limiter_mod
from src.domain.convert.engines.base import CliEngine
from src.domain.files import FilesRuntime, LinkSigner, LocalStore, set_runtime
from src.domain.jobs import CANCELLED, JobsSettings, configure_jobs
from src.domain.operations import pipeline
from src.domain.operations.pipeline import PipelineError, plan
from src.fastmcp_tools import operation_tool
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

KEY = b"p" * 32


def make_pdf(n=2, width=200, height=300) -> bytes:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width, height)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def item(data: bytes, name: str) -> dict:
    return {"file_content": b64(data), "file_name": name}


@pytest.fixture(autouse=True)
def _fresh_jobs_and_limiter():
    saved_jobs, saved_limiter = jobs_mod.get_jobs(), limiter_mod.get_limiter()
    configure_jobs(JobsSettings(wait_sec=10, max_per_owner=5, max_active=64, max_sec=60,
                                keep_sec=3600))
    yield
    jobs_mod._registry = saved_jobs
    limiter_mod._limiter = saved_limiter


def _runtime(tmp_path, mode="link"):
    rt = FilesRuntime(store=LocalStore(tmp_path / "store"),
                      signer=LinkSigner(KEY, "https://files.example"),
                      sweep_interval_sec=60, result_delivery=mode,
                      inline_max_bytes=10 * 1024 * 1024)
    set_runtime(rt)
    return rt


def _client():
    mcp = FastMCP("pipeline-test")
    register_morphyard_tools(mcp)
    return Client(mcp)


def _pdf_blocks(result) -> list[PdfReader]:
    return [PdfReader(io.BytesIO(base64.b64decode(b.resource.blob)))
            for b in result.content if getattr(b, "type", "") == "resource"]


def _count_runs(monkeypatch) -> list[str]:
    """Record every operation the pipeline runs."""
    runs: list[str] = []
    real = pipeline.execute

    async def counting(op, *args, **kwargs):
        runs.append(op.name)
        return await real(op, *args, **kwargs)
    monkeypatch.setattr(pipeline, "execute", counting)
    return runs


class Token:
    def __init__(self, sub):
        self.claims = {"sub": sub}
        self.client_id = "c1"


MERGE_COMPRESS_WATERMARK = [
    {"tool": "pdf_merge"},
    {"tool": "pdf_compress", "args": {"level": "lossless"}},
    {"tool": "pdf_watermark", "args": {"text": "DRAFT"}},
]


# ---------------------------------------------------------------------- running

async def test_merge_compress_watermark_gives_one_file(tmp_path):
    """TC-PIPE-001: merge -> compress -> watermark on 3 PDFs gives one PDF with every page
    (in order) watermarked, and the summary lists each step's result."""
    async with _client() as client:
        r = await client.call_tool("pipeline", {
            "files": [item(make_pdf(1), "a.pdf"), item(make_pdf(2), "b.pdf"),
                      item(make_pdf(3), "c.pdf")],
            "steps": MERGE_COMPRESS_WATERMARK})
    assert not r.is_error
    pdfs = _pdf_blocks(r)
    assert len(pdfs) == 1 and len(pdfs[0].pages) == 6
    assert all("DRAFT" in p.extract_text() for p in pdfs[0].pages)
    text = r.content[0].text
    assert text.startswith("Pipeline done: 3 step(s) on 3 file(s) gave 1 file(s).")
    for k, tool in enumerate(["pdf_merge", "pdf_compress", "pdf_watermark"], start=1):
        assert f"Step {k}/3 {tool}:" in text


async def test_split_then_rotate_fans_out(tmp_path):
    """TC-PIPE-002: split then rotate: the rotate step runs on every part; the parts come back
    as one file each (links in link mode), every page rotated."""
    rt = _runtime(tmp_path, "both")
    async with _client() as client:
        r = await client.call_tool("pipeline", {
            **item(make_pdf(3), "x.pdf"),
            "steps": [{"tool": "pdf_split"}, {"tool": "pdf_rotate", "args": {"degrees": 90}}]})
    assert not r.is_error
    pdfs = _pdf_blocks(r)
    assert len(pdfs) == 3 and all(p.pages[0].rotation == 90 for p in pdfs)
    links = [b for b in r.content if isinstance(b, ResourceLink)]
    assert [link.name for link in links] == ["x_p1_rotated.pdf", "x_p2_rotated.pdf", "x_p3_rotated.pdf"]
    assert "run on each of 3 files" in r.content[0].text
    for link in links:
        assert rt.store.get(link.description.rsplit("file_id ", 1)[1], "anonymous") is not None


async def test_zip_outputs_and_automatic_zip(tmp_path, monkeypatch):
    """TC-PIPE-003: zip_outputs puts the final files into one ZIP named output_name; more
    than MAX_LINKED_OUTPUTS files are zipped without asking."""
    async with _client() as client:
        zipped = await client.call_tool("pipeline", {
            **item(make_pdf(3), "x.pdf"), "zip_outputs": True, "output_name": "parts",
            "steps": [{"tool": "pdf_split"}, {"tool": "pdf_rotate", "args": {"degrees": 180}}]})
        monkeypatch.setattr(pipeline, "MAX_LINKED_OUTPUTS", 2)
        auto = await client.call_tool("pipeline", {
            **item(make_pdf(3), "y.pdf"), "steps": [{"tool": "pdf_split"}]})
    for r, name, stem in ((zipped, "parts.zip", "x_p1_rotated"), (auto, "y_p1_results.zip", "y_p1")):
        files = [b for b in r.content if getattr(b, "type", "") == "resource"]
        assert len(files) == 1 and files[0].resource.uri.endswith(name), files[0].resource.uri
        archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(files[0].resource.blob)))
        assert len(archive.namelist()) == 3 and archive.namelist()[0] == f"{stem}.pdf"
    rotated = PdfReader(io.BytesIO(zipfile.ZipFile(io.BytesIO(base64.b64decode(
        zipped.content[1].resource.blob))).read("x_p2_rotated.pdf")))
    assert rotated.pages[0].rotation == 180


async def test_one_file_step_runs_on_every_input(tmp_path):
    """TC-PIPE-004: a one-to-one first step on several input files runs once per file (batch),
    in order; output_name renames a single final file."""
    async with _client() as client:
        batch = await client.call_tool("pipeline", {
            "files": [item(make_pdf(1), "a.pdf"), item(make_pdf(2), "b.pdf")],
            "steps": [{"tool": "pdf_rotate", "args": {"degrees": 270}}]})
        named = await client.call_tool("pipeline", {
            **item(make_pdf(1), "a.pdf"), "output_name": "final",
            "steps": [{"tool": "pdf_rotate", "args": {"degrees": 90}}]})
    pdfs = _pdf_blocks(batch)
    assert [len(p.pages) for p in pdfs] == [1, 2]
    assert [b.resource.uri.rsplit("/", 1)[1] for b in batch.content[1:]] == \
        ["a_rotated.pdf", "b_rotated.pdf"]
    assert named.content[1].resource.uri.endswith("/final.pdf")


# ---------------------------------------------------------------------- validation

@pytest.mark.parametrize("steps, expected", [
    ([{"tool": "pdf_rotate", "args": {"degrees": 90}}, {"tool": "pdf_teleport"}],
     "Step 2 of 2 (pdf_teleport): 'pdf_teleport' is not a tool a pipeline can run"),
    ([{"tool": "extract_text"}], "Step 1 of 1 (extract_text): 'extract_text' is not a tool"),
    ([{"tool": "pdf_compress"}, {"tool": "pdf_rotate", "args": {"degrees": "ninety"}}],
     "Step 2 of 2 (pdf_rotate): 'degrees': Input should be a valid integer"),
    ([{"tool": "pdf_rotate", "args": {"degrees": 90, "colour": "red"}}],
     "Step 1 of 1 (pdf_rotate): 'colour' is not an argument of this tool"),
    ([{"tool": "pdf_rotate"}], "Step 1 of 1 (pdf_rotate): 'degrees': Field required"),
    ([{"tool": "pdf_rotate", "args": {"degrees": 90, "file_id": "f_x"}}],
     "Step 1 of 1 (pdf_rotate): args gives file_id"),
    ([{"tool": "convert_document", "args": {"target_format": "docx", "output_name": "a"}},
      {"tool": "pdf_compress"}],
     "Step 2 of 2 (pdf_compress) takes pdf, but the step before it produces docx"),
    ([{"tool": "pdf_rotate", "args": {"degrees": 90}},
      {"tool": "convert_document", "args": {"target_format": "mp3", "output_name": "a"}}],
     "Step 2 of 2 (convert_document): 'mp3' is not a target format"),
    ([{"tool": "pdf_watermark", "args": {}}], "Step 1 of 1 (pdf_watermark): "),
    ([{"tool": "pdf_merge"}], "Step 1 of 1 (pdf_merge) needs at least 2 files, but it would get 1"),
    ([{"tool": "pdf_split"}, {"tool": "pdf_merge"}, {"tool": "pdf_merge"}],
     "Step 3 of 3 (pdf_merge) needs at least 2 files"),
])
async def test_invalid_steps_are_refused_before_anything_runs(steps, expected, monkeypatch):
    """TC-PIPE-010: every check made before running names the failing step (and argument),
    and no step runs and no input is read."""
    runs = _count_runs(monkeypatch)
    async with _client() as client:
        r = await client.call_tool("pipeline", {**item(make_pdf(2), "a.pdf"), "steps": steps},
                                   raise_on_error=False)
    assert r.is_error
    assert expected in r.content[0].text, r.content[0].text
    assert "Nothing was run." in r.content[0].text
    assert runs == []


def test_plan_limits_the_number_of_steps():
    """TC-PIPE-011: at most MAX_STEPS (10) steps, at least one (the domain check, also there
    for callers other than the MCP tool)."""
    rotate = {"tool": "pdf_rotate", "args": {"degrees": 90}}
    assert len(plan([rotate] * pipeline.MAX_STEPS, 1)) == 10
    with pytest.raises(PipelineError, match="at most 10 steps; this one has 11"):
        plan([rotate] * 11, 1)
    with pytest.raises(PipelineError, match="at least one step"):
        plan([], 1)


async def test_schema_limits_the_number_of_steps(monkeypatch):
    """TC-PIPE-012: the tool's schema refuses 11 steps (maxItems 10) and none (minItems 1)."""
    runs = _count_runs(monkeypatch)
    rotate = {"tool": "pdf_rotate", "args": {"degrees": 90}}
    async with _client() as client:
        tools = {t.name: t for t in await client.list_tools()}
        too_many = await client.call_tool("pipeline", {**item(make_pdf(1), "a.pdf"),
                                                       "steps": [rotate] * 11},
                                          raise_on_error=False)
    steps = tools["pipeline"].inputSchema["properties"]["steps"]
    assert (steps["minItems"], steps["maxItems"]) == (1, 10)
    assert too_many.is_error and runs == []


def test_plan_format_flow():
    """TC-PIPE-013: the format check follows what each step produces: convert to pdf then a
    PDF tool is fine; pdf_to_images (image or ZIP) then images_to_pdf is fine; a format
    unknown before the first step is checked when it runs, not refused."""
    plan([{"tool": "convert_document", "args": {"target_format": "PDF", "output_name": "a"}},
          {"tool": "pdf_compress"}], 1)
    plan([{"tool": "pdf_to_images"}, {"tool": "images_to_pdf"}], 1)
    plan([{"tool": "images_to_pdf"}, {"tool": "pdf_rotate", "args": {"degrees": 90}}], 3)
    with pytest.raises(PipelineError, match=r"Step 2 of 2 \(pdf_rotate\) takes pdf, but the "
                                            r"step before it produces jpg, png, webp, zip"):
        plan([{"tool": "pdf_to_images"}, {"tool": "pdf_rotate", "args": {"degrees": 90}}], 1)


async def test_input_must_be_given_once(monkeypatch):
    """TC-PIPE-014: no input at all, or files plus file_content, is refused before anything
    runs; a bad files item is named."""
    runs = _count_runs(monkeypatch)
    rotate = [{"tool": "pdf_rotate", "args": {"degrees": 90}}]
    async with _client() as client:
        none = await client.call_tool("pipeline", {"steps": rotate}, raise_on_error=False)
        both = await client.call_tool("pipeline", {
            **item(make_pdf(1), "a.pdf"), "files": [item(make_pdf(1), "b.pdf")],
            "steps": rotate}, raise_on_error=False)
        bad = await client.call_tool("pipeline", {
            "files": [item(make_pdf(1), "b.pdf"), {"file_name": "c.pdf"}], "steps": rotate},
            raise_on_error=False)
    assert none.is_error and "Give the pipeline's input" in none.content[0].text
    assert both.is_error and "either in files or in file_content" in both.content[0].text
    assert bad.is_error and "File #2 in files (c.pdf) gives no file" in bad.content[0].text
    assert runs == []


# ---------------------------------------------------------------------- failures

async def test_a_step_failing_midway_names_the_step_and_keeps_nothing(tmp_path):
    """TC-PIPE-020: a failure known only while running (pdf_merge refuses an encrypted PDF
    made by step 1) names the step, says the earlier step finished, and nothing is kept."""
    _runtime(tmp_path, "link")
    async with _client() as client:
        r = await client.call_tool("pipeline", {
            "files": [item(make_pdf(1), "a.pdf"), item(make_pdf(1), "b.pdf")],
            "steps": [{"tool": "pdf_protect", "args": {"password": "pw-123456"}},
                      {"tool": "pdf_merge"}]}, raise_on_error=False)
        wrong_input = await client.call_tool("pipeline", {
            "file_content": "just text", "file_name": "n.txt",
            "steps": [{"tool": "pdf_rotate", "args": {"degrees": 90}}]}, raise_on_error=False)
    assert r.is_error
    assert "Step 2 of 2 (pdf_merge) failed:" in r.content[0].text
    assert "step 1 had finished; nothing was kept" in r.content[0].text
    assert wrong_input.is_error
    assert "Step 1 of 1 (pdf_rotate) failed: This is not a PDF" in wrong_input.content[0].text
    assert not any((tmp_path / "store").rglob("*.pdf"))


async def test_fan_out_cap(monkeypatch):
    """TC-PIPE-021: a step producing more than MAX_FILES files stops the pipeline, named."""
    monkeypatch.setattr(pipeline, "MAX_FILES", 3)
    async with _client() as client:
        over = await client.call_tool("pipeline", {
            **item(make_pdf(4), "x.pdf"), "steps": [{"tool": "pdf_split"},
                                                    {"tool": "pdf_rotate", "args": {"degrees": 90}}]},
            raise_on_error=False)
        at = await client.call_tool("pipeline", {
            **item(make_pdf(3), "x.pdf"), "steps": [{"tool": "pdf_split"}]})
    assert over.is_error and "Step 1 of 2 (pdf_split) produced 4 files, more than the 3" \
        in over.content[0].text
    assert not at.is_error


# ---------------------------------------------------------------------- owners

async def test_inputs_and_results_follow_the_owner(tmp_path, monkeypatch):
    """TC-PIPE-030: a file_id of another person is refused as unknown (named as the step's
    input); the caller's own file_id works and the result is stored as the caller's."""
    rt = _runtime(tmp_path, "link")
    alice = rt.store.put_bytes("alice", make_pdf(2), name="a.pdf", mime="application/pdf")
    who = {"token": Token("bob")}
    monkeypatch.setattr(operation_tool, "get_access_token", lambda: who["token"])
    steps = [{"tool": "pdf_rotate", "args": {"degrees": 90}}]
    async with _client() as client:
        bob = await client.call_tool("pipeline", {"file_id": alice.file_id, "steps": steps},
                                     raise_on_error=False)
        bob_files = await client.call_tool("pipeline", {
            "files": [{"file_id": alice.file_id}], "steps": steps}, raise_on_error=False)
        who["token"] = Token("alice")
        mine = await client.call_tool("pipeline", {"file_id": alice.file_id, "steps": steps})
    for r in (bob, bob_files):
        assert r.is_error and "is unknown, has expired, or was made by someone else" \
            in r.content[0].text
        assert "Step 1 of 1 (pdf_rotate) failed" in r.content[0].text
    link = [b for b in mine.content if isinstance(b, ResourceLink)][0]
    file_id = link.description.rsplit("file_id ", 1)[1]
    assert rt.store.get(file_id, "alice") is not None
    assert rt.store.get(file_id, "bob") is None


# ---------------------------------------------------------------------- jobs

def _job_id(result) -> str:
    match = re.search(r"job_id (j_[A-Za-z0-9_-]+)", result.content[0].text)
    assert match, result.content[0].text
    return match.group(1)


def _slow_steps(monkeypatch, seconds):
    real = pipeline.execute

    async def slow(*args, **kwargs):
        await asyncio.sleep(seconds)
        return await real(*args, **kwargs)
    monkeypatch.setattr(pipeline, "execute", slow)


async def test_a_long_pipeline_is_one_job(tmp_path, monkeypatch):
    """TC-PIPE-040: a pipeline outliving wait_sec answers with one job_id; job_status gives the
    pipeline's final result; job_list shows one job named pipeline."""
    _runtime(tmp_path, "link")
    configure_jobs(JobsSettings(wait_sec=0.2, max_per_owner=5, max_active=64, max_sec=60,
                                keep_sec=3600))
    _slow_steps(monkeypatch, 0.3)
    async with _client() as client:
        first = await client.call_tool("pipeline", {
            "files": [item(make_pdf(1), "a.pdf"), item(make_pdf(1), "b.pdf")],
            "steps": MERGE_COMPRESS_WATERMARK})
        job_id = _job_id(first)
        listing = await client.call_tool("job_list", {})
        deadline = time.monotonic() + 10
        while True:
            done = await client.call_tool("job_status", {"job_id": job_id, "wait_sec": 20})
            if not done.content[0].text.startswith("Still working") or time.monotonic() > deadline:
                break
    assert [j["tool"] for j in listing.structured_content["jobs"]] == ["pipeline"]
    assert done.content[0].text.startswith("Pipeline done: 3 step(s)")
    links = [b for b in done.content if isinstance(b, ResourceLink)]
    assert len(links) == 1 and links[0].name.startswith("a_merged") \
        and links[0].name.endswith("_watermarked.pdf")


async def test_progress_is_steps_done_of_steps(tmp_path):
    """TC-PIPE-041: with a progressToken the call gets notifications/progress whose total is
    the number of steps, values strictly increasing up to it, messages naming step k of n."""
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async with _client() as client:
        r = await client.call_tool("pipeline", {
            "files": [item(make_pdf(1), "a.pdf"), item(make_pdf(1), "b.pdf")],
            "steps": [{"tool": "pdf_rotate", "args": {"degrees": 90}}, {"tool": "pdf_merge"},
                      {"tool": "pdf_compress", "args": {"level": "lossless"}}]},
            progress_handler=on_progress)
    assert not r.is_error
    values = [p for p, _, _ in seen]
    assert values == sorted(values) and len(set(values)) == len(values)
    assert {t for _, t, _ in seen} == {3}
    assert values[0] == 0 and values[-1] == 3
    messages = [m for _, _, m in seen]
    assert "Step 1 of 3 (pdf_rotate): file 2 of 2" in messages
    assert "Step 2 of 3 (pdf_merge): 2 file(s)" in messages
    assert 0.4 < values[messages.index("Step 1 of 3 (pdf_rotate): file 2 of 2")] < 0.6


async def test_operation_messages_keep_progress_increasing(tmp_path):
    """TC-PIPE-043: the operations' own messages (convert_document's "Converting to ..." and
    "Done") go out between the step events and still get strictly increasing progress."""
    seen: list[tuple[float, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, message))

    async with _client() as client:
        # XML -> Markdown runs on the built-in text engine (no system binary needed in CI)
        r = await client.call_tool("pipeline", {
            "file_content": "<?xml version='1.0'?><note>hello</note>", "file_name": "n.xml",
            "steps": [{"tool": "convert_document",
                       "args": {"target_format": "md", "output_name": "n"}}]},
            progress_handler=on_progress)
    assert not r.is_error, r.content[0].text
    messages = [m for _, m in seen]
    assert messages[0] == "Step 1 of 1 (convert_document)"
    assert "Converting to md..." in messages and any(m.startswith("Done") for m in messages)
    values = [p for p, _ in seen]
    assert values == sorted(values) and len(set(values)) == len(values)
    assert values[-1] == 1


class SleepEngine(CliEngine):
    name = "sleeper"
    binary = "sh"

    async def convert(self, data, src, dst, **opts):  # pragma: no cover - not used
        raise NotImplementedError


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1].split()[0] != "Z"
    except OSError:
        return False


@pytest.mark.skipif(os.name != "posix", reason="process groups")
async def test_cancel_stops_the_running_step(tmp_path, monkeypatch):
    """TC-PIPE-042: job_cancel on a pipeline whose second step runs an engine kills that
    engine's process group; later steps never run; the job is cancelled, nothing kept."""
    _runtime(tmp_path, "link")
    configure_jobs(JobsSettings(wait_sec=0.2, max_per_owner=5, max_active=64, max_sec=60,
                                keep_sec=3600))
    pids = tmp_path / "pids"
    runs: list[str] = []
    real = pipeline.execute

    async def engine_in_step_2(op, *args, **kwargs):
        runs.append(op.name)
        if len(runs) == 2:
            async with limiter_mod.conversion_slot():
                await SleepEngine()._run_command(
                    ["sh", "-c", f"sleep 60 & echo $$ $! > {pids}; wait"], tmp_path, "a", "b",
                    timeout=120)
        return await real(op, *args, **kwargs)
    monkeypatch.setattr(pipeline, "execute", engine_in_step_2)
    async with _client() as client:
        first = await client.call_tool("pipeline", {
            **item(make_pdf(1), "a.pdf"),
            "steps": [{"tool": "pdf_rotate", "args": {"degrees": 90}},
                      {"tool": "pdf_compress"}, {"tool": "pdf_strip_metadata"}]})
        job_id = _job_id(first)
        for _ in range(100):
            if pids.exists() and pids.read_text().strip():
                break
            await asyncio.sleep(0.05)
        shell, child = (int(p) for p in pids.read_text().split())
        assert _alive(shell) and _alive(child)
        cancel = await client.call_tool("job_cancel", {"job_id": job_id})
        status = await client.call_tool("job_status", {"job_id": job_id})
    assert cancel.content[0].text.startswith(f"Cancelled job {job_id} (pipeline)")
    assert "was cancelled" in status.content[0].text
    for _ in range(40):
        if not _alive(child):
            break
        await asyncio.sleep(0.05)
    assert not _alive(shell) and not _alive(child)
    assert runs == ["pdf_rotate", "pdf_compress"]
    assert jobs_mod.get_jobs().get(job_id, "anonymous").state == CANCELLED
    assert limiter_mod.get_limiter().active == 0
    assert not any((tmp_path / "store").rglob("*.pdf"))


# ---------------------------------------------------------------------- guidance

async def test_instructions_and_description_say_when_to_use_it():
    """TC-PIPE-050: the tool description says when to use pipeline rather than separate
    calls, and how to poll; the server instructions mention it."""
    async with _client() as client:
        tool = {t.name: t for t in await client.list_tools()}["pipeline"]
    assert "more than one operation" in tool.description
    assert "job_status" in tool.description
    instructions = open(os.path.join(os.path.dirname(__file__), "..", "config",
                                     "instructions.md"), encoding="utf-8").read()
    assert "`pipeline`" in instructions
