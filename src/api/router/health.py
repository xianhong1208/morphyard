"""Health check endpoints"""

from datetime import datetime

from fastapi import APIRouter

from src.auth.revocation import revocation_status
from src.auth.tool_policy import tool_policy_status
from src.domain.convert.limiter import get_limiter
from src.domain.files import files_status
from src.domain.jobs import get_jobs

router = APIRouter(tags=["Health"], prefix="/health")


@router.get("")
async def health_check():
    """Basic health check, plus the state of MCP Gate's revocation feed.

    `revocation` reports `last_success` (last successful poll), `revoked_count` and `stale`;
    a stale feed makes the status `degraded` (the endpoint still answers 200).
    `conversions` shows how many conversions are running and waiting, and the limits.
    `store` shows how many files are kept and their bytes, against the caps (no names or ids).
    `jobs` shows how many jobs are queued or running and how many finished ones are kept.
    `tool_policy` shows MCP Gate's tool approvals and access rules (`disabled` unless enabled):
    where the policy comes from (`feed`, `cache`, `open`, `closed`) and how many approved
    definitions and rules it holds; `closed` (every tool hidden) makes the status `degraded`.
    """
    revocation = revocation_status()
    tool_policy = tool_policy_status()
    degraded = revocation.get("stale") or tool_policy.get("status") == "degraded"
    return {
        "status": "degraded" if degraded else "healthy",
        "timestamp": datetime.now().astimezone().isoformat(),
        "revocation": revocation,
        "tool_policy": tool_policy,
        "conversions": get_limiter().status(),
        "store": files_status(),
        "jobs": get_jobs().status(),
    }
