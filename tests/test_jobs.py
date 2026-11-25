"""Long conversions as jobs (issue #34).

A call waits up to jobs.wait_sec; a job that is done by then answers inline exactly as
before. A longer one answers with a job_id that job_status / job_cancel / job_list reach,
only for its owner. Jobs wait for a conversion slot in FIFO order without the queue timeout;
caps per owner and in all; a time limit; cancelling kills the engine's processes.
"""

import asyncio
import base64
import dataclasses
import io
import os
import re
import time

import pytest
from fastmcp import Client, FastMCP
from mcp_types import ResourceLink
from pypdf import PdfReader, PdfWriter

from src.domain import jobs as jobs_mod
from src.domain.convert import limiter as limiter_mod
from src.domain.convert.engines.base import CliEngine
from src.domain.files import FilesRuntime, LinkSigner, LocalStore, set_runtime
from src.domain.jobs import (
    CANCELLED,
    COMPLETED,
    QUEUED,
    JobNotFoundError,
    JobRegistry,
    JobsSettings,
    configure_jobs,
)
from src.domain.operations.sources import ANONYMOUS_OWNER
from src.fastmcp_tools import operation_tool
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

KEY = b"j" * 32
ROTATE = {"degrees": 90}


def make_pdf(n=2) -> bytes:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(200, 300)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


PDF_ARGS = {**ROTATE, "file_name": "a.pdf"}


@pytest.fixture(autouse=True)
def _fresh_jobs_and_limiter():
    saved_jobs, saved_limiter = jobs_mod.get_jobs(), limiter_mod.get_limiter()
    yield
    jobs_mod._registry = saved_jobs
    limiter_mod._limiter = saved_limiter


def _settings(**kw) -> JobsSettings:
    base = dict(wait_sec=0.3, max_per_owner=5, max_active=64, max_sec=60, keep_sec=3600)
    base.update(kw)
    return JobsSettings(**base)


def _runtime(tmp_path, mode="link"):
    rt = FilesRuntime(store=LocalStore(tmp_path / "store"),
                      signer=LinkSigner(KEY, "https://files.example"),
                      sweep_interval_sec=60, result_delivery=mode, inline_max_bytes=10 * 1024 * 1024)
    set_runtime(rt)
    return rt


def _client():
    mcp = FastMCP("jobs-test")
    register_morphyard_tools(mcp)
    return Client(mcp)


def _slow(monkeypatch, seconds):
    """Every operation sleeps `seconds` first (holding its job, outside any slot)."""
    real = operation_tool.execute

    async def slow(*args, **kwargs):
        await asyncio.sleep(seconds)
        return await real(*args, **kwargs)
    monkeypatch.setattr(operation_tool, "execute", slow)


async def _wait_done(client, job_id, limit=10.0):
    """job_status until the job is no longer running (each call waits at most wait_sec)."""
    deadline = time.monotonic() + limit
    while True:
        r = await client.call_tool("job_status", {"job_id": job_id, "wait_sec": 30},
                                   raise_on_error=False)
        text = r.content[0].text if r.content and hasattr(r.content[0], "text") else ""
        if not text.startswith("Still working") or time.monotonic() > deadline:
            return r


def _job_id(result) -> str:
    text = result.content[0].text
    match = re.search(r"job_id (j_[A-Za-z0-9_-]+)", text)
    assert match, text
    return match.group(1)


class Token:
    def __init__(self, sub):
        self.claims = {"sub": sub}
        self.client_id = "c1"


def _as(monkeypatch, who: dict):
    monkeypatch.setattr(operation_tool, "get_access_token", lambda: who["token"])


# ---------------------------------------------------------------------- inline vs job

async def test_a_quick_call_answers_inline_and_leaves_no_job(tmp_path):
    """TC-JOB-001: a conversion done within wait_sec answers as before jobs existed: the 1.3
    inline reply, no job id, and no job is kept (job_list is empty)."""
    configure_jobs(_settings(wait_sec=10))
    async with _client() as client:
        result = await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())})
        listing = await client.call_tool("job_list", {})
    assert [type(b).__name__ for b in result.content] == ["TextContent", "EmbeddedResource"]
    assert "job_id" not in result.content[0].text
    assert listing.structured_content["jobs"] == []
    assert jobs_mod.get_jobs().active() == []


async def test_a_long_call_returns_a_job_and_job_status_the_result(tmp_path, monkeypatch):
    """TC-JOB-002: a call that outlives wait_sec answers with a job_id and how to poll;
    job_status waits for the job and returns the reply the tool would have given (link mode:
    summary with the link, and a resource_link), and gives it again when asked again."""
    rt = _runtime(tmp_path, "link")
    configure_jobs(_settings(wait_sec=0.2))
    _slow(monkeypatch, 0.6)
    async with _client() as client:
        started = time.monotonic()
        first = await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())})
        assert time.monotonic() - started < 0.55
        job_id = _job_id(first)
        assert "job_status" in first.content[0].text
        assert len(first.content) == 1
        done = await _wait_done(client, job_id)
        again = await client.call_tool("job_status", {"job_id": job_id, "wait_sec": 0})
    for r in (done, again):
        assert not r.is_error
        links = [b for b in r.content if isinstance(b, ResourceLink)]
        assert len(links) == 1 and links[0].name == "a_rotated.pdf"
        assert "Rotated" in r.content[0].text and "Download link" in r.content[0].text
    stored = rt.store.get(links[0].description.rsplit("file_id ", 1)[1], ANONYMOUS_OWNER)
    assert stored is not None


async def test_job_status_wait_is_capped(tmp_path, monkeypatch):
    """TC-JOB-003: job_status never waits longer than jobs.wait_sec, whatever wait_sec asks;
    a job still running is answered with its handle again."""
    configure_jobs(_settings(wait_sec=0.3))
    _slow(monkeypatch, 3)
    async with _client() as client:
        job_id = _job_id(await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())}))
        started = time.monotonic()
        status = await client.call_tool("job_status", {"job_id": job_id, "wait_sec": 100})
        waited = time.monotonic() - started
    assert waited < 1.5
    assert _job_id(status) == job_id and "Still working" in status.content[0].text


async def test_inline_mode_job_result_is_the_1_3_reply(tmp_path, monkeypatch):
    """TC-JOB-004: under `inline` a job's result comes back from job_status as the 1.3 reply
    ([summary, embedded file], no link), the same bytes the tool makes."""
    _runtime(tmp_path, "inline")
    configure_jobs(_settings(wait_sec=0.1))
    _slow(monkeypatch, 0.3)
    async with _client() as client:
        job_id = _job_id(await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())}))
        done = await _wait_done(client, job_id)
    assert [type(b).__name__ for b in done.content] == ["TextContent", "EmbeddedResource"]
    assert "Download link" not in done.content[0].text
    pdf = PdfReader(io.BytesIO(base64.b64decode(done.content[1].resource.blob)))
    assert pdf.pages[0].rotation == 90


async def test_report_tool_job_returns_the_text(tmp_path, monkeypatch):
    """TC-JOB-005: extract_text as a job: job_status returns the extracted text itself."""
    configure_jobs(_settings(wait_sec=0.1))
    _slow(monkeypatch, 0.3)
    async with _client() as client:
        first = await client.call_tool("extract_text", {"file_content": "# Title\n\nhello jobs",
                                                         "file_name": "n.md"})
        job_id = _job_id(first)
        done = await _wait_done(client, job_id)
    assert "hello jobs" in done.content[0].text and "job_id" not in done.content[0].text


async def test_a_failed_job_answers_like_the_tool(tmp_path, monkeypatch):
    """TC-JOB-006: a refusal (not a PDF) comes back from job_status as the same refusal
    text the tool gives inline."""
    configure_jobs(_settings(wait_sec=0.1))
    _slow(monkeypatch, 0.3)
    async with _client() as client:
        refused_id = _job_id(await client.call_tool("pdf_rotate", {**ROTATE, "file_content": "plain text",
                                                                    "file_name": "a.txt"}))
        refused = await _wait_done(client, refused_id)
        configure_jobs(_settings(wait_sec=10))
        inline_refusal = await client.call_tool("pdf_rotate", {**ROTATE, "file_content": "plain text",
                                                                "file_name": "a.txt"}, raise_on_error=False)
    assert refused.content[0].text == inline_refusal.content[0].text
    assert "PDF" in refused.content[0].text


async def test_an_engine_error_in_a_job_is_the_same_tool_error(tmp_path, monkeypatch):
    """TC-JOB-007: an exception in a job is raised again by job_status (same message)."""
    configure_jobs(_settings(wait_sec=0.1))

    async def broken(*args, **kwargs):
        await asyncio.sleep(0.3)
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(operation_tool, "execute", broken)
    async with _client() as client:
        job_id = _job_id(await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())}))
        status = await _wait_done(client, job_id)
    assert status.is_error and "engine exploded" in status.content[0].text


# ---------------------------------------------------------------------- owners

async def test_another_owner_cannot_see_or_cancel_a_job(tmp_path, monkeypatch):
    """TC-JOB-010: bob gets "unknown" for alice's job (the same answer as for an id that
    never existed) from job_status and job_cancel, and job_list shows him nothing; alice's
    job is untouched and alice gets its result."""
    _runtime(tmp_path, "link")
    configure_jobs(_settings(wait_sec=0.1))
    _slow(monkeypatch, 0.5)
    who = {"token": Token("alice")}
    _as(monkeypatch, who)
    async with _client() as client:
        job_id = _job_id(await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())}))
        who["token"] = Token("bob")
        peek = await client.call_tool("job_status", {"job_id": job_id}, raise_on_error=False)
        kill = await client.call_tool("job_cancel", {"job_id": job_id}, raise_on_error=False)
        unknown = await client.call_tool("job_status", {"job_id": "j_" + "A" * 22}, raise_on_error=False)
        bob_list = await client.call_tool("job_list", {})
        who["token"] = Token("alice")
        alice_list = await client.call_tool("job_list", {})
        done = await _wait_done(client, job_id)
    assert peek.is_error and kill.is_error and unknown.is_error
    def message(r):
        return r.content[0].text.split("': ", 1)[1]  # without "Error calling tool 'x'"
    assert message(peek) == message(kill)
    assert message(peek).replace(job_id, "ID") == message(unknown).replace("j_" + "A" * 22, "ID")
    assert bob_list.structured_content["jobs"] == []
    assert [j["job_id"] for j in alice_list.structured_content["jobs"]] == [job_id]
    assert not done.is_error and any(isinstance(b, ResourceLink) for b in done.content)


def test_registry_get_checks_the_owner():
    """TC-JOB-011: JobRegistry.get raises JobNotFoundError for another owner's job."""
    reg = JobRegistry(_settings())
    job = jobs_mod.Job(id="j_x", owner="alice", tool="t", created_at=time.time())
    reg._jobs[job.id] = job
    assert reg.get("j_x", "alice") is job
    with pytest.raises(JobNotFoundError):
        reg.get("j_x", "bob")
    assert reg.list("bob") == []


# ---------------------------------------------------------------------- caps

async def test_per_owner_cap(tmp_path, monkeypatch):
    """TC-JOB-012: an owner with max_per_owner jobs active is refused (too_many_jobs) at once,
    nothing started; another owner still gets through."""
    configure_jobs(_settings(wait_sec=0.05, max_per_owner=2))
    _slow(monkeypatch, 1.0)
    who = {"token": Token("alice")}
    _as(monkeypatch, who)
    args = {**PDF_ARGS, "file_content": b64(make_pdf())}
    async with _client() as client:
        for _ in range(2):
            _job_id(await client.call_tool("pdf_rotate", args))
        third = await client.call_tool("pdf_rotate", args, raise_on_error=False)
        who["token"] = Token("bob")
        bobs = await client.call_tool("pdf_rotate", args, raise_on_error=False)
    assert third.is_error and "the most this server allows per person" in third.content[0].text
    assert not bobs.is_error
    assert len(jobs_mod.get_jobs().active("alice")) == 2


async def test_global_cap(tmp_path, monkeypatch):
    """TC-JOB-013: with max_active jobs active on the server, a new one is refused as busy."""
    configure_jobs(_settings(wait_sec=0.05, max_active=2))
    _slow(monkeypatch, 1.0)
    who = {"token": Token("u0")}
    _as(monkeypatch, who)
    args = {**PDF_ARGS, "file_content": b64(make_pdf())}
    async with _client() as client:
        for i in range(2):
            who["token"] = Token(f"u{i}")
            _job_id(await client.call_tool("pdf_rotate", args))
        who["token"] = Token("u9")
        refused = await client.call_tool("pdf_rotate", args, raise_on_error=False)
    assert refused.is_error and "server is busy" in refused.content[0].text


async def test_job_time_limit(tmp_path, monkeypatch):
    """TC-JOB-014: a job is stopped after max_sec; job_status says so."""
    configure_jobs(_settings(wait_sec=0.05, max_sec=0.3))
    _slow(monkeypatch, 5)
    async with _client() as client:
        job_id = _job_id(await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())}))
        started = time.monotonic()
        status = await client.call_tool("job_status", {"job_id": job_id, "wait_sec": 0.3},
                                        raise_on_error=False)
        while not status.is_error and time.monotonic() - started < 3:
            status = await client.call_tool("job_status", {"job_id": job_id, "wait_sec": 0.3},
                                            raise_on_error=False)
    assert status.is_error and "took longer than 0.3 seconds" in status.content[0].text


# ---------------------------------------------------------------------- queue

async def test_jobs_wait_in_the_queue_without_the_queue_timeout(tmp_path):
    """TC-JOB-020: with every slot taken, jobs queue FIFO beyond queue_timeout_sec (no
    server_busy), report their queue position, and run in arrival order once slots free up."""
    lim = limiter_mod.configure(1, 5, 0.1)
    reg = configure_jobs(_settings())
    order: list[int] = []

    def work_for(i):
        async def work(job):
            async with limiter_mod.conversion_slot():
                order.append(i)
            return i
        return work

    async def keep(job, result):
        return result

    await lim.acquire()
    jobs = [reg.submit("o", "t", work_for(i), keep) for i in range(3)]
    for j in jobs:
        reg.detach(j)
    await asyncio.sleep(0.4)  # well past the 0.1 s queue timeout
    assert [j.state for j in jobs] == [QUEUED] * 3
    assert [j.position() for j in jobs] == [1, 2, 3]
    assert "position 2 in the queue" in jobs[1].events[0].message
    lim.release()
    await asyncio.wait([j.task for j in jobs], timeout=5)
    assert [j.state for j in jobs] == [COMPLETED] * 3
    assert order == [0, 1, 2]
    assert lim.active == 0 and lim.queued == 0


async def test_a_full_queue_refuses_a_job_at_once(tmp_path):
    """TC-JOB-021: beyond max_queued a tool call is answered busy at once, not queued."""
    lim = limiter_mod.configure(1, 0, 30)
    configure_jobs(_settings(wait_sec=5))
    await lim.acquire()
    try:
        async with _client() as client:
            started = time.monotonic()
            busy = await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())},
                                          raise_on_error=False)
            assert time.monotonic() - started < 2
    finally:
        lim.release()
    assert busy.is_error and "server is busy" in busy.content[0].text
    assert lim.active == 0


# ---------------------------------------------------------------------- cancel

def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


class SleepEngine(CliEngine):
    name = "sleeper"
    binary = "sh"

    async def convert(self, data, src, dst, **opts):  # pragma: no cover - not used
        raise NotImplementedError


@pytest.mark.skipif(os.name != "posix", reason="process groups")
async def test_cancel_kills_the_engine_and_the_queue_advances(tmp_path):
    """TC-JOB-030: job_cancel on a running job kills its engine's whole process group (the
    shell and the child it started), frees the slot, and the next queued job runs."""
    lim = limiter_mod.configure(1, 5, 30)
    reg = configure_jobs(_settings())
    pids = tmp_path / "pids"

    async def engine_work(job):
        async with limiter_mod.conversion_slot():
            await SleepEngine()._run_command(
                ["sh", "-c", f"sleep 60 & echo $$ $! > {pids}; wait"], tmp_path, "a", "b", timeout=120)
        return "never"

    async def next_work(job):
        async with limiter_mod.conversion_slot():
            return "next"

    async def keep(job, result):
        return result

    running = reg.submit("o", "convert_document", engine_work, keep)
    reg.detach(running)
    queued = reg.submit("o", "pdf_rotate", next_work, keep)
    reg.detach(queued)
    for _ in range(100):
        if pids.exists() and pids.read_text().strip():
            break
        await asyncio.sleep(0.05)
    shell, child = (int(p) for p in pids.read_text().split())
    assert _alive(shell) and _alive(child)
    assert queued.state == QUEUED
    await reg.cancel(running)
    assert running.state == CANCELLED
    for _ in range(40):
        if not _alive(child):
            break
        await asyncio.sleep(0.05)
    assert not _alive(shell) and not _alive(child)
    await asyncio.wait([queued.task], timeout=5)
    assert queued.state == COMPLETED and queued.kept == "next"
    assert lim.active == 0 and lim.queued == 0


async def test_job_cancel_tool(tmp_path, monkeypatch):
    """TC-JOB-031: job_cancel stops a running job (job_status then says cancelled); on a
    finished job it changes nothing and says so."""
    configure_jobs(_settings(wait_sec=0.05))
    _slow(monkeypatch, 5)
    async with _client() as client:
        job_id = _job_id(await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())}))
        cancel = await client.call_tool("job_cancel", {"job_id": job_id})
        status = await client.call_tool("job_status", {"job_id": job_id})
        again = await client.call_tool("job_cancel", {"job_id": job_id})
        listing = await client.call_tool("job_list", {})
    assert cancel.content[0].text.startswith(f"Cancelled job {job_id}")
    assert "was cancelled" in status.content[0].text
    assert "already finished (cancelled)" in again.content[0].text
    assert listing.structured_content["jobs"][0]["status"] == CANCELLED


async def test_a_cancelled_request_cancels_its_unhanded_job(tmp_path, monkeypatch):
    """TC-JOB-032: when the request itself is cancelled while waiting inline (no job_id was
    handed out), its job is cancelled too and forgotten."""
    reg = configure_jobs(_settings(wait_sec=10))
    _slow(monkeypatch, 5)
    op = operation_tool.get_operation("pdf_rotate")
    sources = operation_tool.upload().sources({"file_content": b64(make_pdf()), "file_name": "a.pdf"},
                                              True, "o")
    call = asyncio.create_task(operation_tool.run_as_job(
        op, "pdf_rotate", operation_tool.Reply.FILES, dict(ROTATE), sources, "o", None))
    await asyncio.sleep(0.2)
    job = reg.active("o")[0]
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.wait([job.task], timeout=5)
    await asyncio.sleep(0)
    assert job.state == CANCELLED
    assert reg.list("o") == []


async def test_shutdown_cancels_unfinished_jobs():
    """TC-JOB-033: at shutdown every unfinished job is cancelled."""
    reg = configure_jobs(_settings())

    async def forever(job):
        await asyncio.sleep(60)

    async def keep(job, result):
        return result

    job = reg.submit("o", "t", forever, keep)
    await asyncio.sleep(0.05)
    async with jobs_mod.jobs_lifespan():
        pass
    assert job.state == CANCELLED


# ---------------------------------------------------------------------- progress

async def test_progress_notifications_with_a_progress_token(tmp_path, monkeypatch):
    """TC-JOB-040: a call with a progressToken gets notifications/progress with increasing
    values for the job's steps (convert_document's messages); job_status forwards the
    steps that happen while it waits."""
    configure_jobs(_settings(wait_sec=5))
    seen: list[tuple[float, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, message))

    async with _client() as client:
        result = await client.call_tool("convert_document", {
            "target_format": "html", "output_name": "n", "file_content": "# Hi\n\ntext",
            "file_name": "n.md"}, progress_handler=on_progress)
    assert not result.is_error
    assert len(seen) >= 2
    values = [p for p, _ in seen]
    assert values == sorted(values) and len(set(values)) == len(values)
    assert any(m and m.startswith("Done") for _, m in seen)


# ---------------------------------------------------------------------- expiry

async def test_finished_jobs_are_forgotten_after_keep_sec(tmp_path, monkeypatch):
    """TC-JOB-050: a finished job is kept for keep_sec (the file TTL), then job_status says
    it is unknown; its result files expire with the store's TTL and job_status says so."""
    rt = _runtime(tmp_path, "link")
    clock = {"now": time.time()}
    reg = JobRegistry(_settings(wait_sec=0.05, keep_sec=100), clock=lambda: clock["now"])
    jobs_mod._registry = reg
    _slow(monkeypatch, 0.2)
    async with _client() as client:
        job_id = _job_id(await client.call_tool("pdf_rotate", {**PDF_ARGS, "file_content": b64(make_pdf())}))
        await asyncio.wait([reg._jobs[job_id].task], timeout=5)
        kept = reg._jobs[job_id].kept.kept[0]
        rt.store._index[kept.meta.file_id] = dataclasses.replace(kept.meta, expires_at=time.time() - 1)
        expired = await client.call_tool("job_status", {"job_id": job_id}, raise_on_error=False)
        clock["now"] += 101
        gone = await client.call_tool("job_status", {"job_id": job_id}, raise_on_error=False)
    assert expired.is_error and "has expired" in expired.content[0].text
    assert gone.is_error and "is unknown" in gone.content[0].text


# ---------------------------------------------------------------------- config

def test_jobs_config_defaults_and_limits():
    """TC-JOB-060: defaults 20 s / 5 per person / 64 in all / 1800 s; wait_sec at most 120."""
    from pydantic import ValidationError

    from src.config.model import JobsConfig
    cfg = JobsConfig()
    assert (cfg.wait_sec, cfg.max_per_owner, cfg.max_active, cfg.max_sec) == (20, 5, 64, 1800)
    with pytest.raises(ValidationError):
        JobsConfig(wait_sec=121)
    with pytest.raises(ValidationError):
        JobsConfig(max_per_owner=0)


def test_job_tools_tell_models_to_poll():
    """TC-JOB-061: every tool that runs as a job says how to poll; the quick read-only tools
    do not."""
    async def tools():
        async with _client() as client:
            return {t.name: t.description for t in await client.list_tools()}
    described = asyncio.run(tools())
    for name, text in described.items():
        if name in ("list_supported_conversions", "inspect_document", "pdf_metadata",
                    "job_status", "job_cancel", "job_list"):
            assert operation_tool.JOB_HINT not in text, name
        else:
            assert f"\n\n{operation_tool.JOB_HINT}" in text, name
