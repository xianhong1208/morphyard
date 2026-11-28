"""MCP Gate tool approvals, access rules and step-up on Morphyard (`auth.tool_policy`, off by default).

The vendored hooks file (src/auth/mcp_gate_hooks.py) must reproduce MCP Gate's golden vectors, copied to
tests/fixtures/mcp_gate/. The app tests build the real app (real Morphyard tools, real FastMCP transport,
tokens signed here and verified by the real verifier) and fake only MCP Gate's side of the policy feed.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import stat
import time
import uuid
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from src.auth import mcp_gate_hooks as hooks
from src.auth import tool_policy as tpm

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HASH_VECTORS = json.loads((FIXTURES / "mcp_gate" / "tool_hash_vectors.json").read_text("utf-8"))
POLICY_VECTORS = json.loads((FIXTURES / "mcp_gate" / "tool_policy_vectors.json").read_text("utf-8"))
SNAPSHOT_NAMES = [t["name"] for t in json.loads((FIXTURES / "mcp_tools.json").read_text("utf-8"))]

ISSUER = "http://gate.test:4568"
BASE_URL = "http://morphyard.test:5050"
AUDIENCE = f"{BASE_URL}/mcp"
REFS = {"auth.revocation.client_secret": "MORPHYARD_REVOCATION_CLIENT_SECRET"}
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {}}


# -- the vendored copy reproduces MCP Gate ----------------------------------------------------------


@pytest.mark.parametrize("vector", HASH_VECTORS["vectors"], ids=lambda v: v["label"])
def test_vendored_hooks_reproduce_the_hash_vectors(vector):
    """TC-TPOL-001: every tool definition hashes exactly as MCP Gate pins it."""
    assert hooks.canonical_json(vector["tool"]) == vector["canonical"]
    assert hooks.tool_hash(vector["tool"]) == vector["sha256"]
    assert hooks.HASH_VERSION == HASH_VECTORS["version"]


@pytest.mark.parametrize("vector", POLICY_VECTORS["vectors"], ids=lambda v: v["label"])
def test_vendored_hooks_reproduce_the_policy_vectors(vector):
    """TC-TPOL-002: every access verdict is exactly MCP Gate's (rules_version included)."""
    got = hooks.evaluate_access(vector["policy"], vector["token"], vector["tool"],
                                approved=vector["approved"], now=vector["now"])
    assert got == vector["expect"]
    assert hooks.RULES_VERSION == POLICY_VECTORS["rules_version"]


# -- configuration ----------------------------------------------------------------------------------


def _config(auth_enabled=True, *, rev=None, data_dir=None, **tp):
    from src.config.model import RevocationConfig, ToolPolicyConfig
    return SimpleNamespace(
        auth=SimpleNamespace(enabled=auth_enabled, revocation=RevocationConfig(**(rev or {})),
                             tool_policy=ToolPolicyConfig(**tp)),
        files=SimpleNamespace(data_dir=data_dir))


CREDS = {"client_id": "rs-client", "client_secret": "s3cret"}


def test_config_yaml_ships_the_tool_policy_off():
    """TC-TPOL-003: the shipped config turns it off by default, every value from an env variable."""
    import yaml
    from src.config.config_manager import env_refs
    raw = yaml.safe_load((pathlib.Path(__file__).parent.parent / "config" / "config.yaml")
                         .read_text(encoding="utf-8"))
    tp = raw["auth"]["tool_policy"]
    assert tp["enabled"] == "${MORPHYARD_TOOL_POLICY_ENABLED:-false}"
    assert tp["fail_mode"] == "${MORPHYARD_TOOL_POLICY_FAIL_MODE:-closed}"
    assert env_refs(raw)["auth.tool_policy.poll_interval_sec"] == "MORPHYARD_TOOL_POLICY_POLL_INTERVAL"
    from src.config.model import AuthConfig
    assert AuthConfig().tool_policy.enabled is False
    assert AuthConfig().tool_policy.fail_mode == "closed"


def test_build_is_off_by_default(tmp_path):
    """TC-TPOL-004: nothing is built while the setting is off (even with credentials present)."""
    assert tpm.build_tool_policy(_config(rev=CREDS, data_dir=str(tmp_path)), issuer=ISSUER,
                                 audience=AUDIENCE, env_refs=REFS) is None
    assert tpm.build_tool_policy(SimpleNamespace(), issuer=ISSUER, audience=AUDIENCE) is None
    assert not (tmp_path / tpm.CACHE_FILE_NAME).exists()


@pytest.mark.parametrize("auth_enabled, rev, refs, match", [
    (False, CREDS, REFS, "authentication is disabled"),
    (True, {}, REFS, "client_id / client_secret are empty"),
    (True, {"client_id": "x"}, REFS, "client_id / client_secret are empty"),
    (True, CREDS, {}, "must be an \\$\\{ENV_VAR\\} reference"),
])
def test_build_refuses_a_setup_that_cannot_enforce(tmp_path, auth_enabled, rev, refs, match):
    """TC-TPOL-005: switched on but unusable stops the start instead of running without the policy."""
    with pytest.raises(RuntimeError, match=match):
        tpm.build_tool_policy(_config(auth_enabled, rev=rev, data_dir=str(tmp_path), enabled=True),
                              issuer=ISSUER if auth_enabled else None,
                              audience=AUDIENCE if auth_enabled else None, env_refs=refs)


def test_config_model_rejects_an_unknown_fail_mode():
    """TC-TPOL-006: fail_mode is open or closed."""
    from pydantic import ValidationError
    from src.config.model import ToolPolicyConfig
    with pytest.raises(ValidationError):
        ToolPolicyConfig(fail_mode="maybe")
    with pytest.raises(ValidationError):
        ToolPolicyConfig(poll_interval_sec=1)


async def test_build_uses_the_revocation_client_and_a_cache_in_the_data_dir(tmp_path):
    """TC-TPOL-007: the revocation feed's client polls /oauth/policy; the cache lives in data_dir;
    fail_mode closed by default, open on request; the revocation feed itself may stay off."""
    data_dir = tmp_path / "data"
    tp = tpm.build_tool_policy(_config(rev=CREDS, data_dir=str(data_dir), enabled=True),
                               issuer=ISSUER + "/", audience=AUDIENCE, env_refs=REFS)
    try:
        assert tp.feed.client_id == "rs-client" and tp.feed.center == ISSUER
        assert tp.pinning.cache_file == os.fspath(data_dir / tpm.CACHE_FILE_NAME)
        assert tp.pinning.fail_open is False and tp.pinning.state == "closed"
        assert tp.pinning.poll_interval == 15
    finally:
        await tp.close()
    tp = tpm.build_tool_policy(_config(rev=CREDS, data_dir=str(data_dir), enabled=True,
                                       fail_mode="open", poll_interval_sec=40),
                               issuer=ISSUER, audience=AUDIENCE, env_refs=REFS)
    try:
        assert tp.pinning.fail_open is True and tp.pinning.state == "open"
        assert tp.pinning.poll_interval == 40
    finally:
        await tp.close()


async def test_feed_client_authenticates_with_the_client_credentials():
    """TC-TPOL-008: the form carries the client's credentials plus the middleware's fields."""
    seen = []

    def handler(request):
        seen.append((str(request.url), dict(httpx.QueryParams(request.content.decode()))))
        return httpx.Response(200, json={"ok": True})
    client = tpm.PolicyFeedClient(issuer=ISSUER, audience=AUDIENCE, client_id="rs", client_secret="pw",
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client._post("/oauth/policy", {"features": "rules1"}) == {"ok": True}
    assert seen == [(f"{ISSUER}/oauth/policy",
                     {"client_id": "rs", "client_secret": "pw", "features": "rules1"})]


# -- the app ----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key, *, sub="alice", scope="mcp:tools", client_id="claude", jti="j-1"):
    now = int(time.time())
    return jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 600,
                       "jti": jti, "scope": scope, "client_id": client_id, "token_use": "access"},
                      key, algorithm="RS256")


class FakeGate:
    """MCP Gate's side of /oauth/policy and /oauth/tool-report."""

    def __init__(self):
        self.approved: set[str] = set()
        self.access: dict | None = None   # None: answer format 1 (no rules)
        self.down = False
        self.requests: list[tuple[str, dict]] = []

    def policy(self, rules=(), tool_scopes=None, unscoped="allow"):
        self.access = {"rules_version": 1, "unscoped_tools": unscoped,
                       "tool_scopes": tool_scopes or {}, "rules": list(rules)}

    async def post(self, client, path, form):
        self.requests.append((path, dict(form)))
        if self.down:
            raise httpx.ConnectError("MCP Gate is down")
        if path == "/oauth/tool-report":
            tools = json.loads(form["tools"])
            return {"recorded": [{"name": t["name"], "hash": t["hash"], "state": "pending", "new": True}
                                 for t in tools], "invalid": [], "hash_mismatch": []}
        assert path == "/oauth/policy"
        body = {"format": 2 if self.access else 1, "hash_version": 1, "audience": AUDIENCE,
                "service_id": "svc", "rules": [], "unchanged": False, "etag": uuid.uuid4().hex,
                "now": int(time.time()),
                "tools": {"approved": sorted(self.approved), "pending": [], "rejected": []}}
        if self.access:
            body.update(self.access)
        return body


def _build_app(monkeypatch, rsa_key, gate, data_dir, *, enabled=True, fail_mode="closed"):
    import app as app_module
    from fastmcp.server.auth import RemoteAuthProvider
    from src.auth.fastmcp_verifier import RevocationAwareJWTVerifier
    from src.config.config_manager import Config
    from src.config.model import (AppConfig, AuthConfig, ConfigModel, FilesConfig, LoggingConfig,
                                  ModulesConfig, RevocationConfig, ServerConfig, ToolPolicyConfig)
    pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    # Tokens signed here, verified by the real verifier (instead of fetching MCP Gate's JWKS).
    monkeypatch.setattr(app_module, "build_auth_provider", lambda cfg: RemoteAuthProvider(
        token_verifier=RevocationAwareJWTVerifier(public_key=pem, issuer=ISSUER, audience=AUDIENCE),
        authorization_servers=[ISSUER], base_url=BASE_URL))
    module = SimpleNamespace(mcp_tools=SimpleNamespace(
        module="src.fastmcp_tools.morphyard_tools", function_name="register_morphyard_tools"))
    monkeypatch.setattr(Config, "get_module_model", staticmethod(lambda name: module))
    monkeypatch.setattr(Config, "_env_refs", dict(REFS))
    monkeypatch.setattr(tpm.PolicyFeedClient, "_post", lambda self, path, form: gate.post(self, path, form))
    config = ConfigModel(
        server=ServerConfig(host="127.0.0.1", port=5050, transport="http"),
        auth=AuthConfig(enabled=True, issuer=ISSUER, base_url=BASE_URL,
                        revocation=RevocationConfig(client_id="rs-client", client_secret="s3cret"),
                        tool_policy=ToolPolicyConfig(enabled=enabled, fail_mode=fail_mode)),
        logging=LoggingConfig(level="WARNING"),
        app=AppConfig(name="Morphyard", version="1", title="Morphyard", description=""),
        modules=ModulesConfig(enabled=["morphyard"]),
        files=FilesConfig(data_dir=str(data_dir)),
    )
    return app_module.create_app(config, transport="http")


def _hashes() -> dict[str, str]:
    mcp = tpm.get_tool_policy().server
    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    return {t.name: hooks.tool_hash(hooks.mcp_tool_dict(t)) for t in tools}


def _rpc(c, token, method, params=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if params and "name" in params:
        headers["Mcp-Name"] = params["name"]
    return c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), "_meta": META}})


def _names(c, token):
    r = _rpc(c, token, "tools/list")
    assert r.status_code == 200, r.text
    return [t["name"] for t in r.json()["result"]["tools"]]


def _call(c, token, name, arguments=None):
    return _rpc(c, token, "tools/call", {"name": name, "arguments": arguments or {}})


def _error_text(r) -> str:
    result = r.json()["result"]
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


@pytest.fixture(autouse=True)
def _no_tool_policy_left_over():
    saved = tpm.get_tool_policy()
    yield
    tpm.set_tool_policy(saved)


def test_off_lists_every_tool_and_never_asks_mcp_gate(monkeypatch, rsa_key, tmp_path):
    """TC-TPOL-009: off (the default) = as before: every tool listed and callable for any valid
    token, no request to MCP Gate's policy feed, no cache file, /health says disabled."""
    gate = FakeGate()
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path, enabled=False)
    assert tpm.get_tool_policy() is None
    with TestClient(app, base_url=BASE_URL) as c:
        token = _token(rsa_key)
        assert _names(c, token) == SNAPSHOT_NAMES
        r = _call(c, token, "list_supported_conversions")
        assert r.status_code == 200 and not r.json()["result"].get("isError")
        health = c.get("/health").json()
    assert health["tool_policy"] == {"status": "disabled", "enabled": False}
    assert gate.requests == []
    assert not (tmp_path / tpm.CACHE_FILE_NAME).exists()


def test_on_lists_only_approved_tools_and_reports_the_rest(monkeypatch, rsa_key, tmp_path):
    """TC-TPOL-010: a tool whose definition MCP Gate has not approved is hidden, refused like an
    unknown tool, and reported; the policy is cached in data_dir with mode 600."""
    gate = FakeGate()
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path)
    hashes = _hashes()
    gate.approved = {h for n, h in hashes.items() if n != "pdf_protect"}
    with TestClient(app, base_url=BASE_URL) as c:
        token = _token(rsa_key)
        names = _names(c, token)
        assert "pdf_protect" not in names and len(names) == len(SNAPSHOT_NAMES) - 1
        assert _error_text(_call(c, token, "pdf_protect")) == "Tool 'pdf_protect' is not available on this server."
        c.portal.call(tpm.get_tool_policy().pinning.flush_reports)
        health = c.get("/health").json()["tool_policy"]
    policy_polls = [form for path, form in gate.requests if path == "/oauth/policy"]
    assert policy_polls and all(form["features"] == "rules1" for form in policy_polls)
    reported = [json.loads(form["tools"]) for path, form in gate.requests if path == "/oauth/tool-report"]
    assert [t["name"] for batch in reported for t in batch] == ["pdf_protect"]
    assert health["state"] == "feed" and health["approved_count"] == len(SNAPSHOT_NAMES) - 1
    cache = tmp_path / tpm.CACHE_FILE_NAME
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


def test_on_a_deny_rule_hides_the_tool_from_that_token_only_within_one_poll(monkeypatch, rsa_key, tmp_path):
    """TC-TPOL-011: a deny rule for a user hides the tool from that user's tokens (call refused as
    unknown), not from others; removing it applies at the next poll."""
    gate = FakeGate()
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path)
    gate.approved = set(_hashes().values())
    gate.policy(rules=[{"id": "r1", "effect": "deny", "subject": {"kind": "user", "id": "mallory"},
                        "tools": ["list_supported_conversions"], "expires_at": None}])
    with TestClient(app, base_url=BASE_URL) as c:
        mallory, alice = _token(rsa_key, sub="mallory"), _token(rsa_key, sub="alice")
        assert "list_supported_conversions" not in _names(c, mallory)
        assert "list_supported_conversions" in _names(c, alice)
        assert "not available" in _error_text(_call(c, mallory, "list_supported_conversions"))
        assert not _call(c, alice, "list_supported_conversions").json()["result"].get("isError")
        gate.policy(rules=[])
        c.portal.call(tpm.get_tool_policy().pinning.poll_policy)
        assert "list_supported_conversions" in _names(c, mallory)


def test_on_a_missing_scope_gets_an_http_403_step_up(monkeypatch, rsa_key, tmp_path):
    """TC-TPOL-012: a tool in a scope the token lacks is listed; its call gets HTTP 403
    insufficient_scope naming that scope (the step-up gate), and runs with the scope."""
    gate = FakeGate()
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path)
    gate.approved = set(_hashes().values())
    gate.policy(tool_scopes={"list_supported_conversions": {"any_of": ["morphyard:admin"],
                                                            "challenge": "morphyard:admin"}})
    with TestClient(app, base_url=BASE_URL) as c:
        plain, admin = _token(rsa_key), _token(rsa_key, scope="mcp:tools morphyard:admin")
        assert "list_supported_conversions" in _names(c, plain)
        r = _call(c, plain, "list_supported_conversions")
        assert r.status_code == 403
        challenge = r.headers["www-authenticate"]
        assert challenge.startswith('Bearer error="insufficient_scope", scope="morphyard:admin"')
        assert f'resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource/mcp"' in challenge
        assert r.json()["error"]["data"] == {"error": "insufficient_scope", "scope": "morphyard:admin"}
        ok = _call(c, admin, "list_supported_conversions")
        assert ok.status_code == 200 and not ok.json()["result"].get("isError")


def test_on_fail_closed_hides_every_tool_until_mcp_gate_answers(monkeypatch, rsa_key, tmp_path):
    """TC-TPOL-013: never reached and no cache: every tool hidden and /health degraded (closed)."""
    gate = FakeGate()
    gate.down = True
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path)
    with TestClient(app, base_url=BASE_URL) as c:
        token = _token(rsa_key)
        assert _names(c, token) == []
        assert "not available" in _error_text(_call(c, token, "list_supported_conversions"))
        health = c.get("/health").json()
    assert health["status"] == "degraded" and health["tool_policy"]["state"] == "closed"


def test_on_fail_open_lists_every_tool_until_mcp_gate_answers(monkeypatch, rsa_key, tmp_path):
    """TC-TPOL-014: fail_mode open: never reached = every tool listed and callable."""
    gate = FakeGate()
    gate.down = True
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path, fail_mode="open")
    with TestClient(app, base_url=BASE_URL) as c:
        assert _names(c, _token(rsa_key)) == SNAPSHOT_NAMES
        assert c.get("/health").json()["tool_policy"]["state"] == "open"


def test_the_cached_policy_survives_a_restart_while_mcp_gate_is_down(monkeypatch, rsa_key, tmp_path):
    """TC-TPOL-015: the last approvals and rules keep applying after a restart without MCP Gate."""
    gate = FakeGate()
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path)
    gate.approved = set(_hashes().values())
    gate.policy(rules=[{"id": "r1", "effect": "deny", "subject": {"kind": "user", "id": "alice"},
                        "tools": ["pdf_rotate"], "expires_at": None}])
    with TestClient(app, base_url=BASE_URL) as c:
        before = _names(c, _token(rsa_key))
    assert "pdf_rotate" not in before
    gate.down = True
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path)
    with TestClient(app, base_url=BASE_URL) as c:
        assert _names(c, _token(rsa_key)) == before
        assert c.get("/health").json()["tool_policy"]["state"] == "cache"


def test_a_pipeline_step_obeys_the_rules_of_its_tool(monkeypatch, rsa_key, tmp_path):
    """TC-TPOL-016: a pipeline cannot run a tool the caller may not call directly: a denied step or one
    whose definition is not approved is refused as unavailable, a step missing a scope names the scope;
    allowed steps pass the check."""
    gate = FakeGate()
    app = _build_app(monkeypatch, rsa_key, gate, tmp_path)
    gate.approved = {h for n, h in _hashes().items() if n != "pdf_watermark"}
    gate.policy(rules=[{"id": "r1", "effect": "deny", "subject": {"kind": "user", "id": "alice"},
                        "tools": ["pdf_rotate"], "expires_at": None}],
                tool_scopes={"pdf_compress": {"any_of": ["morphyard:write"], "challenge": "morphyard:write"}})
    args = {"pdf_rotate": {"degrees": 90}, "pdf_compress": {}, "pdf_watermark": {"text": "DRAFT"}}
    steps = lambda *tools: {"steps": [{"tool": t, "args": args[t]} for t in tools]}  # noqa: E731
    with TestClient(app, base_url=BASE_URL) as c:
        alice, bob = _token(rsa_key, sub="alice"), _token(rsa_key, sub="bob")
        denied = _error_text(_call(c, alice, "pipeline", steps("pdf_rotate")))
        assert denied.endswith("Step 1 of 1 (pdf_rotate): Tool 'pdf_rotate' is not available on this server.")
        scoped = _error_text(_call(c, bob, "pipeline", steps("pdf_rotate", "pdf_compress")))
        assert "Step 2 of 2 (pdf_compress): Tool 'pdf_compress' requires the scope 'morphyard:write'" in scoped
        unapproved = _error_text(_call(c, bob, "pipeline", steps("pdf_watermark")))
        assert unapproved.endswith("Step 1 of 1 (pdf_watermark): Tool 'pdf_watermark' is not available on this server.")
        # Allowed steps get past the check: the pipeline then asks for its input file.
        assert "Give the pipeline's input" in _error_text(_call(c, bob, "pipeline", steps("pdf_rotate")))
