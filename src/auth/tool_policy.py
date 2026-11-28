"""MCP Gate tool approvals and tool access rules on Morphyard's tools (opt-in, off by default).

With ``auth.tool_policy.enabled`` (``MORPHYARD_TOOL_POLICY_ENABLED=true``) Morphyard runs MCP Gate's own
server-side middleware:

* ``MCPGateToolPinning`` (a FastMCP middleware) lists and runs only the tool definitions an admin approved
  in the MCP Gate console, reports new or changed ones so they wait there as pending, and applies the
  server's access rules to each token: a tool denied to the caller is hidden and refused like an unknown
  tool; a tool that needs a scope the token lacks is listed and its call refused naming the scope.
* ``MCPGateScopeGate`` (an ASGI middleware in front of FastMCP) answers that last case with HTTP 403
  ``insufficient_scope`` so a client that supports step-up asks the user for the scope and retries.

Both come from ``src/auth/mcp_gate_hooks.py``, an unmodified copy of MCP Gate's
``examples/mcp_gate_hooks.py`` (see ``HOOKS_SOURCE``). Its tests reproduce MCP Gate's golden vectors
(``tests/fixtures/mcp_gate/``), so the hashes and verdicts here are exactly MCP Gate's. To update it,
copy the newer file and both fixtures from MCP Gate and run the tests.

The policy feed (``POST <issuer>/oauth/policy``) and tool reports take the same resource-server client
as the revocation feed, so its credentials are ``auth.revocation.client_id`` / ``client_secret`` whether
or not the revocation feed itself is enabled. The last policy received is cached in
``<files.data_dir>/mcp_gate_policy.json`` (mode 600), so a restart while MCP Gate is down keeps it.
``fail_mode`` decides what happens before any policy was received: ``closed`` (default, as MCP Gate
recommends) hides every tool, ``open`` lists and runs every tool until the first answer.

A pipeline runs other tools as steps; each step is checked as if its tool were called directly, so a
rule on a tool cannot be bypassed through ``pipeline``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import httpx
from fastmcp.exceptions import ToolError
from starlette.middleware import Middleware

from src.auth import mcp_gate_hooks as hooks
from src.log import get_logger

logger = get_logger("auth")

HOOKS_SOURCE = "xianhong1208/MCP_Gate examples/mcp_gate_hooks.py at a02af22 (MCP Gate 1.6 development)"
CACHE_FILE_NAME = "mcp_gate_policy.json"
FAIL_OPEN = "open"
FAIL_CLOSED = "closed"
REQUEST_TIMEOUT_SEC = 10.0


class PolicyFeedClient:
    """The part of ``MCPGateVerifier`` that ``MCPGateToolPinning`` uses: MCP Gate's URL, this server's
    audience and an authenticated form POST with the resource-server client.

    Morphyard keeps its own token verifier and revocation poller (``src/auth/revocation.py``), so it
    gives the middleware this small client instead of a second verifier.
    """

    def __init__(self, *, issuer: str, audience: str, client_id: str, client_secret: str,
                 http_client: Optional[httpx.AsyncClient] = None):
        self.center = issuer.rstrip("/")
        self.audience = audience
        self.client_id = client_id
        self._client_secret = client_secret
        self._own_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)

    async def _post(self, path: str, form: dict) -> dict:
        r = await self._http.post(f"{self.center}{path}",
                                  data={"client_id": self.client_id,
                                        "client_secret": self._client_secret, **form})
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        if self._own_client:
            await self._http.aclose()


class ToolPolicy:
    """The pinning middleware, its feed client, and the ASGI step-up gate for one FastMCP server."""

    def __init__(self, *, feed: PolicyFeedClient, cache_file: str, fail_mode: str,
                 poll_interval: float):
        self.feed = feed
        self.fail_mode = fail_mode
        self.pinning = hooks.MCPGateToolPinning(
            feed, cache_file=cache_file, fail_open=fail_mode == FAIL_OPEN,
            poll_interval=poll_interval)
        self.server: Any = None

    def install(self, mcp) -> list[Middleware]:
        """Add the pinning middleware to ``mcp``; returns the ASGI middleware for ``mcp.http_app``."""
        self.server = mcp
        mcp.add_middleware(self.pinning)
        return [Middleware(hooks.MCPGateScopeGate, pinning=self.pinning, server=mcp)]

    async def check_steps(self, tool_names: Iterable[str]) -> None:
        """Refuse a pipeline whose steps the caller could not call directly (ToolError naming the step).

        The same checks as ``tools/call``: the step's tool definition must be approved, a rule must not
        hide it from this token, and the token must carry one of its scopes.
        """
        pinning = self.pinning
        await pinning._ensure_started()
        claims = pinning._token_claims()
        names = list(tool_names)
        for number, name in enumerate(names, start=1):
            label = f"Step {number} of {len(names)} ({name})"
            tool = await self.server.get_tool(name) if self.server is not None else None
            digest = pinning._note(tool) if tool is not None else None
            if digest is None or not pinning.is_allowed(digest):
                logger.warning(f"Refused a pipeline step: tool {name!r} is not approved in MCP Gate "
                               f"({pinning.state})")
                raise ToolError(f"{label}: {hooks.NOT_AVAILABLE.format(name=name)}")
            verdict = pinning.access(name, claims)
            if verdict["decision"] == hooks.ACCESS_HIDDEN:
                logger.warning(f"Refused a pipeline step: tool {name!r} for client "
                               f"{(claims or {}).get('client_id')}: {verdict['reason']}")
                raise ToolError(f"{label}: {hooks.NOT_AVAILABLE.format(name=name)}")
            if verdict["decision"] == hooks.ACCESS_NEEDS_SCOPE:
                raise ToolError(f"{label}: {hooks.scope_refusal(name, verdict['challenge'], claims)}")

    def status(self) -> dict[str, Any]:
        """Health view: where the policy comes from and how much of it there is (no names, no hashes)."""
        pinning = self.pinning
        policy = pinning._policy
        access = pinning.access_policy
        state = pinning.state
        return {
            # "closed": no policy yet and fail_mode closed, so every tool is hidden.
            "status": "degraded" if state == "closed" else "healthy",
            "enabled": True,
            "state": state,
            "fail_mode": self.fail_mode,
            "poll_interval_sec": pinning.poll_interval,
            "approved_count": len(policy["approved"]) if policy else 0,
            "pending_count": len(policy["pending"]) if policy else 0,
            "access_rules": access is not None,
            "rule_count": len(access["rules"]) if access else 0,
            "scoped_tool_count": len(access["tool_scopes"]) if access else 0,
        }

    async def close(self) -> None:
        await self.pinning.close()
        await self.feed.aclose()


# -- configuration ------------------------------------------------------------------


def build_tool_policy(config, *, issuer: Optional[str], audience: Optional[str],
                      env_refs: Optional[Mapping[str, str]] = None,
                      http_client: Optional[httpx.AsyncClient] = None) -> Optional[ToolPolicy]:
    """Build the tool policy from ``config.auth.tool_policy``; None when it is off (the default).

    Raises RuntimeError on a misconfiguration (on without authentication or without the resource-server
    client, a literal client secret) so the server refuses to start rather than run without the policy
    an admin believes is enforced.
    """
    auth = getattr(config, "auth", None)
    tp = getattr(auth, "tool_policy", None)
    if tp is None or not getattr(tp, "enabled", False):
        return None
    if not getattr(auth, "enabled", False) or not issuer or not audience:
        raise RuntimeError(
            "auth.tool_policy.enabled is true but authentication is disabled: access rules match the "
            "caller's token, so enable auth (AUTH_ENABLED=true) or turn the tool policy off")
    rev = getattr(auth, "revocation", None)
    client_id = (getattr(rev, "client_id", None) or "").strip()
    client_secret = (getattr(rev, "client_secret", None) or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "auth.tool_policy.enabled is true but auth.revocation.client_id / client_secret are empty: "
            "the policy feed uses the revocation feed's resource-server client (register a confidential "
            "client in MCP Gate, bind it to this server, and set MORPHYARD_REVOCATION_CLIENT_ID / _SECRET)")
    if env_refs is not None and "auth.revocation.client_secret" not in env_refs:
        raise RuntimeError(
            "auth.revocation.client_secret must be an ${ENV_VAR} reference, not a literal value "
            "(e.g. client_secret: \"${MORPHYARD_REVOCATION_CLIENT_SECRET}\")")
    fail_mode = str(getattr(tp, "fail_mode", FAIL_CLOSED) or FAIL_CLOSED)  # the model allows open | closed
    files = getattr(config, "files", None)
    data_dir = Path(getattr(files, "data_dir", None) or "./data")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    feed = PolicyFeedClient(issuer=str(issuer), audience=str(audience), client_id=client_id,
                            client_secret=client_secret, http_client=http_client)
    return ToolPolicy(feed=feed, cache_file=os.fspath(data_dir / CACHE_FILE_NAME),
                      fail_mode=fail_mode,
                      poll_interval=float(getattr(tp, "poll_interval_sec", 15) or 15))


# -- module state ---------------------------------------------------------------------

_tool_policy: Optional[ToolPolicy] = None


def set_tool_policy(tp: Optional[ToolPolicy]) -> None:
    global _tool_policy
    _tool_policy = tp


def get_tool_policy() -> Optional[ToolPolicy]:
    return _tool_policy


async def check_pipeline_steps(tool_names: Iterable[str]) -> None:
    """Refuse pipeline steps the caller may not call (no-op while the tool policy is off)."""
    tp = _tool_policy
    if tp is not None:
        await tp.check_steps(tool_names)


def tool_policy_status() -> dict[str, Any]:
    """Health view of the tool policy (``{"status": "disabled"}`` when off)."""
    tp = _tool_policy
    if tp is None:
        return {"status": "disabled", "enabled": False}
    return tp.status()
