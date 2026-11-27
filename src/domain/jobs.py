"""Long conversions as jobs (issue #34).

Every tool call that does conversion work runs as a job: a task of its own, owned by the
caller (the token's `sub`). The call waits up to `wait_sec` (20 s by default) for it; a job that
finishes in time is answered inline exactly as before and leaves no trace here. A job that does
not is handed back as a job id, and `job_status` / `job_cancel` / `job_list` reach it later.

What this module does not know: MCP. The tool layer (src/fastmcp_tools/) decides how a result
is kept once the caller has left (`Job.keep`) and how it is shown.

Jobs live in memory. A restart loses them (their engine processes die with the server anyway);
files a finished job already put in the store stay until they expire.

Rules a caller can rely on:
  - a job is only ever seen by its owner: any other owner gets the same answer as for an
    unknown id (`get`, `cancel` and `list` filter by owner);
  - an owner has at most `max_per_owner` jobs queued or running, the server `max_active`;
  - a job ends after `max_sec` at the latest, its wait for a slot included;
  - cancelling a job cancels its task; engines kill their process group on cancellation
    (engines/base.py), and the conversion slot is released by the `async with` it sits in;
  - a finished job is kept for `keep_sec` (the file store's TTL: its files expire then too).
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from src.domain.convert.limiter import ConversionLimiter, ServerBusyError, waiting_as_job
from src.domain.exceptions import DomainException
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED = "queued", "running", "completed", "failed", "cancelled"
FINISHED = frozenset({COMPLETED, FAILED, CANCELLED})

DEFAULT_WAIT_SEC = 20.0
DEFAULT_MAX_PER_OWNER = 5
DEFAULT_MAX_ACTIVE = 64
DEFAULT_MAX_SEC = 1800
DEFAULT_KEEP_SEC = 3600
# How long job_cancel waits for the job's task (and its engine) to be gone.
CANCEL_WAIT_SEC = 10.0


def new_job_id() -> str:
    return "j_" + secrets.token_urlsafe(16)


class JobNotFoundError(DomainException):
    """An id that is unknown, finished too long ago, lost in a restart, or someone else's."""

    def __init__(self, job_id: str):
        shown = job_id if isinstance(job_id, str) and job_id.startswith("j_") and len(job_id) < 40 \
            else repr(str(job_id)[:40])
        super().__init__(
            f"Job {shown} is unknown: it may have finished too long ago, been lost when the "
            "server restarted (jobs are kept in memory), or have been started by someone "
            "else. Call job_list to see your jobs, or run the original tool again.",
            "job_not_found",
        )


class TooManyJobsError(DomainException):
    def __init__(self, limit: int):
        super().__init__(
            f"You already have {limit} conversions queued or running, the most this server "
            "allows per person. No new conversion was started. Wait for one to finish (call "
            "job_status on a job_id from job_list), then call this tool again with the same "
            "arguments.",
            "too_many_jobs",
            details={"limit": limit},
        )


class JobTimeoutError(DomainException):
    def __init__(self, max_sec: float):
        super().__init__(
            f"The conversion took longer than {max_sec:g} seconds (waiting in the queue "
            "included) and was stopped. Nothing was kept. Try a smaller file, or tell the user "
            "the server is too busy for this conversion right now.",
            "job_timeout",
            details={"max_sec": max_sec},
        )


@dataclass(frozen=True)
class JobsSettings:
    wait_sec: float = DEFAULT_WAIT_SEC
    max_per_owner: int = DEFAULT_MAX_PER_OWNER
    max_active: int = DEFAULT_MAX_ACTIVE
    max_sec: float = DEFAULT_MAX_SEC
    keep_sec: float = DEFAULT_KEEP_SEC


@dataclass
class Event:
    """One progress step: `message` for people; `operation` is True for the operation's own
    messages (the ones a tool used to send as log notifications). `progress` / `total` are
    the job's measured progress when it has one (a pipeline: steps done of steps), else None."""
    message: str
    operation: bool = False
    progress: float | None = None
    total: float | None = None


@dataclass(eq=False)
class Job:
    id: str
    owner: str
    tool: str
    created_at: float
    state: str = RUNNING
    started_at: float | None = None
    finished_at: float | None = None
    expires_at: float | None = None
    events: list[Event] = field(default_factory=list)
    # Set when the caller stops waiting: from then on the result is kept (`keep`) for
    # job_status instead of being handed to the caller.
    detached: bool = False
    # The operation's raw result, while a waiting caller has still to take it.
    result: Any = None
    # What `keep` made of the result for job_status (the tool layer's business).
    kept: Any = None
    error: BaseException | None = None
    task: asyncio.Task | None = None
    _ticket: asyncio.Future | None = None
    _limiter: ConversionLimiter | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)
    # (done, total) once the work reports measured progress (set_progress); every later event
    # carries it.
    _progress: tuple[float, float] | None = None

    # ------------------------------------------------------------ state

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    def position(self) -> int | None:
        """Place in the conversion queue while queued, else None."""
        if self.state != QUEUED or self._ticket is None or self._limiter is None:
            return None
        return self._limiter.position_of(self._ticket)

    def _touch(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def add_event(self, message: str, operation: bool = False) -> None:
        done, total = self._progress if self._progress is not None else (None, None)
        self.events.append(Event(message, operation, done, total))
        self._touch()

    async def set_progress(self, done: float, total: float, message: str) -> None:
        """Measured progress (a pipeline's steps): `done` of `total`, with a message."""
        self._progress = (done, total)
        self.add_event(message)

    async def say(self, message: str) -> None:
        """The operation's progress callback (OpCall.notify)."""
        self.add_event(message, operation=True)

    # SlotObserver (limiter.py)
    def waiting(self, limiter: ConversionLimiter, ticket: asyncio.Future) -> None:
        self._limiter, self._ticket, self.state = limiter, ticket, QUEUED
        position = limiter.position_of(ticket)
        self.add_event(f"Waiting for a free conversion slot (position {position} in the queue)")
        logger.info(f"[jobs] {self.id[:8]} {self.tool} queued at position {position}")

    def started(self) -> None:
        was_queued = self.state == QUEUED
        self._ticket = None
        if not self.finished:
            self.state = RUNNING
        if self.started_at is None:
            self.started_at = time.time()
        if was_queued:
            self.add_event("Conversion started")
            logger.info(f"[jobs] {self.id[:8]} {self.tool} started after "
                        f"{self.started_at - self.created_at:.1f}s in the queue")

    def describe(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        out: dict[str, Any] = {
            "job_id": self.id,
            "tool": self.tool,
            "status": self.state,
            "created_at": _iso(self.created_at),
            "seconds": round((self.finished_at or now) - self.created_at, 1),
        }
        position = self.position()
        if position is not None:
            out["queue_position"] = position
        if self.events:
            out["last_message"] = self.events[-1].message
        if self.expires_at is not None:
            out["kept_until"] = _iso(self.expires_at)
        return out

    # ------------------------------------------------------------ waiting

    async def wait(self, timeout: float,
                   on_event: Callable[[int, Event], Awaitable[None]] | None = None,
                   seen: int = 0) -> bool:
        """Wait up to `timeout` seconds for the job to finish; True when it has.

        `on_event(n, event)` is called for every event after the first `seen`, in order, with
        n = its 1-based number (it only grows: usable as an MCP progress value). Events that
        happen before the job finishes are all passed on before this returns.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            if on_event is not None:
                while seen < len(self.events):
                    seen += 1
                    try:
                        await on_event(seen, self.events[seen - 1])
                    except Exception as e:  # a lost client must not break the job
                        logger.debug(f"[jobs] progress not delivered: {e}")
                        on_event = None
                        break
            if self.finished:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                pass


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# The work of a job: given the job (for its progress callback), return the operation's result.
Work = Callable[[Job], Awaitable[Any]]
# What to do with a result once the caller has left: returns what job_status will show.
Keep = Callable[[Job, Any], Awaitable[Any]]


class JobRegistry:
    """The jobs of this server process."""

    def __init__(self, settings: JobsSettings | None = None, clock: Callable[[], float] = time.time):
        self.settings = settings or JobsSettings()
        self._clock = clock
        self._jobs: dict[str, Job] = {}

    # ------------------------------------------------------------ bookkeeping

    def _prune(self) -> None:
        now = self._clock()
        for job_id in [j.id for j in self._jobs.values()
                       if j.finished and j.expires_at is not None and j.expires_at <= now]:
            del self._jobs[job_id]

    def active(self, owner: str | None = None) -> list[Job]:
        return [j for j in self._jobs.values()
                if not j.finished and (owner is None or j.owner == owner)]

    def get(self, job_id: str, owner: str) -> Job:
        """The owner's job, or JobNotFoundError (same error for another owner's job)."""
        self._prune()
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None or job.owner != owner:
            raise JobNotFoundError(job_id)
        return job

    def list(self, owner: str) -> list[Job]:
        self._prune()
        return sorted((j for j in self._jobs.values() if j.owner == owner),
                      key=lambda j: j.created_at, reverse=True)

    def discard(self, job: Job) -> None:
        """Forget a job whose result its caller took inline (it was never handed out)."""
        self._jobs.pop(job.id, None)

    def status(self) -> dict:
        self._prune()
        active = self.active()
        return {
            "active": len(active),
            "queued": sum(1 for j in active if j.state == QUEUED),
            "kept": len(self._jobs) - len(active),
            "wait_sec": self.settings.wait_sec,
            "max_per_owner": self.settings.max_per_owner,
            "max_active": self.settings.max_active,
            "max_sec": self.settings.max_sec,
        }

    # ------------------------------------------------------------ running

    def submit(self, owner: str, tool: str, work: Work, keep: Keep) -> Job:
        """Start a job for `owner` (TooManyJobsError / ServerBusyError when over a cap)."""
        self._prune()
        if len(self.active(owner)) >= self.settings.max_per_owner:
            logger.warning(f"[jobs] refused {tool}: owner already has "
                           f"{self.settings.max_per_owner} active jobs")
            raise TooManyJobsError(self.settings.max_per_owner)
        active = len(self.active())
        if active >= self.settings.max_active:
            logger.warning(f"[jobs] refused {tool}: {active} jobs active, the most this server runs")
            raise ServerBusyError(f"{active} conversions are queued or running, the most this "
                                  "server takes", 30)
        job = Job(id=new_job_id(), owner=owner, tool=tool, created_at=self._clock())
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, work, keep), name=f"job {job.id}")
        return job

    async def _run(self, job: Job, work: Work, keep: Keep) -> None:
        try:
            with waiting_as_job(job):
                timeout = asyncio.timeout(self.settings.max_sec)
                try:
                    async with timeout:
                        result = await work(job)
                        if job.detached:
                            job.kept = await keep(job, result)
                        else:
                            job.result = result
                except TimeoutError:
                    if not timeout.expired():
                        raise
                    raise JobTimeoutError(self.settings.max_sec) from None
            job.state = COMPLETED
        except asyncio.CancelledError:
            job.state = CANCELLED
            logger.info(f"[jobs] {job.id[:8]} {job.tool} cancelled")
        except BaseException as e:  # noqa: BLE001 - kept for the caller / job_status
            job.state = FAILED
            job.error = e
            if job.detached:
                logger.info(f"[jobs] {job.id[:8]} {job.tool} failed: {e}")
        finally:
            job.finished_at = self._clock()
            job.expires_at = job.finished_at + self.settings.keep_sec
            job._ticket = None
            job._touch()

    def detach(self, job: Job) -> None:
        """The caller stopped waiting with the job unfinished: keep its result for job_status."""
        job.detached = True
        logger.info(f"[jobs] {job.id[:8]} {job.tool} continues in the background")

    async def cancel(self, job: Job) -> None:
        """Cancel the job and wait (up to CANCEL_WAIT_SEC) until its task, and so its engine
        process, is gone."""
        if job.task is None or job.task.done():
            return
        job.task.cancel()
        await asyncio.wait({job.task}, timeout=CANCEL_WAIT_SEC)

    async def shutdown(self) -> None:
        """Cancel every unfinished job (server shutdown: engines must not outlive it)."""
        tasks = [j.task for j in self.active() if j.task is not None and not j.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=CANCEL_WAIT_SEC)


_registry = JobRegistry()


def configure_jobs(settings: JobsSettings) -> JobRegistry:
    """Replace the process-wide registry (at startup)."""
    global _registry
    _registry = JobRegistry(settings)
    return _registry


def get_jobs() -> JobRegistry:
    return _registry


def init_jobs(config) -> JobRegistry:
    """Build the registry from `config.jobs`; finished jobs are kept as long as files."""
    jobs = config.jobs
    files = getattr(config, "files", None)
    registry = configure_jobs(JobsSettings(
        wait_sec=jobs.wait_sec, max_per_owner=jobs.max_per_owner, max_active=jobs.max_active,
        max_sec=jobs.max_sec, keep_sec=getattr(files, "ttl_sec", DEFAULT_KEEP_SEC),
    ))
    logger.info(
        f"Jobs: a call waits {jobs.wait_sec:g}s before answering with a job id; at most "
        f"{jobs.max_per_owner} per person and {jobs.max_active} in all at once; stopped after "
        f"{jobs.max_sec:g}s; kept in memory"
    )
    return registry


@asynccontextmanager
async def jobs_lifespan():
    """At shutdown, cancel unfinished jobs so no engine process outlives the server."""
    try:
        yield
    finally:
        await _registry.shutdown()
