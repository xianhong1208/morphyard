"""job_status, job_cancel and job_list: reaching a conversion that outlived its call (issue #34).

A job is only ever visible to its owner (the token's `sub`): another person's job id gets the
same "unknown" answer as an id that never existed (src/domain/jobs.py).
"""

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from src.domain.jobs import get_jobs
from src.fastmcp_tools.operation_tool import (
    _REPLIES,
    Reply,
    current_owner,
    finished_reply,
    handle_reply,
    progress_forwarder,
)
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

JOB_ID = Annotated[str, Field(description="The job_id a Morphyard tool returned, e.g. 'j_...'")]


def register_job_tools(mcp: FastMCP) -> None:
    @mcp.tool(output_schema=None)
    async def job_status(
        job_id: JOB_ID,
        wait_sec: Annotated[float, Field(
            description="How long to wait for the job to finish before answering, in seconds. "
                        "The server caps it (20 s by default); it answers as soon as the job "
                        "is done.")] = 20,
    ):
        """Get the result of a conversion that was still running when its tool answered.

        When a Morphyard tool replies that the work is still running and gives a job_id,
        call this with that job_id. It waits for the job (up to wait_sec) and then returns
        exactly what the original tool would have returned: the converted file(s) with their
        download links, or the text. If the job is still running it says so again: call it
        again with the same job_id. Results are kept for a limited time after the job ends.
        """
        jobs = get_jobs()
        job = jobs.get(job_id, current_owner())
        reply = _REPLIES.get(job.tool, Reply.FILES)
        wait = min(max(0.0, float(wait_sec)), jobs.settings.wait_sec)
        done = await job.wait(wait, progress_forwarder(), seen=len(job.events))
        if not done:
            return handle_reply(job, reply, jobs.settings.wait_sec)
        return await finished_reply(job, reply)

    @mcp.tool(output_schema=None)
    async def job_cancel(job_id: JOB_ID) -> str:
        """Stop a conversion that is still queued or running.

        Use it when the user no longer wants the result, or the job was started by mistake.
        The conversion engine is stopped at once and nothing is kept. A job that has already
        finished is left as it is.
        """
        jobs = get_jobs()
        job = jobs.get(job_id, current_owner())
        if job.finished:
            return (f"Job {job.id} ({job.tool}) has already finished ({job.state}); there is "
                    "nothing to cancel. Call job_status to get its result.")
        await jobs.cancel(job)
        logger.info(f"[jobs] {job.id[:8]} {job.tool} cancelled by its owner")
        if job.state == "cancelled":
            return (f"Cancelled job {job.id} ({job.tool}): the conversion was stopped and "
                    "nothing was kept.")
        return (f"Job {job.id} ({job.tool}) ended as {job.state} before it could be "
                "cancelled. Call job_status to see its result.")

    @mcp.tool()
    async def job_list() -> dict[str, Any]:
        """List your conversion jobs: the ones still queued or running, and the finished ones
        whose results are still kept.

        Use it to find a job_id you lost track of. Each entry gives the job_id, the tool,
        its status (queued, running, completed, failed or cancelled) and how long it has run.
        Jobs are kept in memory: a server restart forgets them.
        """
        jobs = get_jobs()
        owner = current_owner()
        return {
            "jobs": [job.describe() for job in jobs.list(owner)],
            "max_jobs_at_once": jobs.settings.max_per_owner,
        }

    logger.info("Job tools registered")
