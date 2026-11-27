"""Pydantic configuration models.

Defines the Pydantic model for every config section. To add a custom section, define
its model here and add it to ConfigModel.
"""


import os
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ServerConfig(BaseModel):
    """Server settings."""
    host: str
    port: int
    transport: str = "http"


class RevocationConfig(BaseModel):
    """MCP Gate's revocation feed (`auth.revocation`).

    Morphyard verifies tokens offline, so without this a token revoked in MCP Gate keeps
    working until it expires. When enabled, a background task polls
    `<issuer>/oauth/revoked` and the /mcp verifier rejects listed tokens. The
    credentials belong to a confidential client registered in the MCP Gate console and
    bound to this server as its resource server.
    """
    enabled: bool = False
    client_id: str | None = None
    # Must be an ${ENV_VAR} reference; a literal secret in config.yaml is refused at startup.
    client_secret: str | None = Field(None, repr=False)
    poll_interval_sec: float = Field(30, ge=5, le=3600)
    # "open": keep accepting tokens when the feed is unreachable (logged loudly);
    # "closed": reject every token once no poll has succeeded for
    # stale_after_intervals x poll_interval_sec.
    fail_mode: Literal["open", "closed"] = "open"
    stale_after_intervals: int = Field(4, ge=2, le=100)


class AuthConfig(BaseModel):
    """Bearer-token verification against an OAuth 2.1 authorization server (MCP Gate).

    Tokens are RS256 JWTs verified offline through the issuer's JWKS; no per-request
    network call is needed once the keys are cached. `audience` must match the resource
    URI registered for this server in MCP Gate (by default its MCP URL).
    """
    enabled: bool = True
    issuer: str | None = None
    audience: str | None = None
    base_url: str | None = None
    required_scopes: list[str] = []
    # MCP Gate revocation feed (off by default; needs a resource-server client).
    revocation: RevocationConfig = Field(default_factory=RevocationConfig)


class LoggingConfig(BaseModel):
    """Logging settings."""
    level: str = "INFO"
    format: str = "colorized"
    file: str | None = None
    max_size: str = "10MB"
    backup_count: int = 3
    log_dir: str = "logs"


class ConvertConfig(BaseModel):
    """Conversion settings.

    max_input_mb drives two things at once: the file-size check in the service layer
    and the request-body limit of the MCP streamable HTTP transport (see
    _raise_mcp_body_limit in app.py). They must move together: change only one and
    either the transport rejects the file with a bare 413 (the AI never sees a useful
    message) or the service-layer check is never reached.
    """
    max_input_mb: int = 50
    # Seconds allowed per engine call. None (ENGINE_TIMEOUT unset) keeps each engine's own
    # default: LibreOffice 180, Pandoc 120, OCR 300. A value applies to every engine.
    engine_timeout_sec: Optional[int] = Field(default=None, gt=0)
    max_hops: int = 3
    # How many conversions run at once (src/domain/convert/limiter.py). None
    # (MORPHYARD_MAX_CONCURRENT_CONVERSIONS unset) = the CPUs this process may use, at most 8.
    max_concurrent_conversions: Optional[int] = Field(default=None, gt=0)
    # How many more may wait for a slot. None = 4 x max_concurrent_conversions; 0 = no queue.
    max_queued_conversions: Optional[int] = Field(default=None, ge=0)
    # Longest wait for a slot before the request is refused as busy.
    queue_timeout_sec: float = Field(default=30, gt=0)

    @field_validator("engine_timeout_sec", "max_concurrent_conversions",
                     "max_queued_conversions", mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        # config.yaml writes ${ENGINE_TIMEOUT:-} and the like, which arrive as "" when unset
        return None if v is None or (isinstance(v, str) and not v.strip()) else v


class FilesConfig(BaseModel):
    """Files kept on the server and their download links (`files:`, issue #33).

    Results (and, later, uploads and job outputs) are stored under `data_dir/store` for
    `ttl_sec` and handed out as signed, expiring links under `public_url`. When a store cap is
    reached new files are refused; unexpired files are never evicted.
    """
    # Directory for the store and the generated signing key (Docker: the /app/data volume).
    # config.yaml sets it; the fallback reads the same variable for configs built in code.
    data_dir: str = Field(default_factory=lambda: os.environ.get("MORPHYARD_DATA_DIR") or "./data")
    # Address people's browsers and agents reach this server at; empty = auth.base_url.
    public_url: str | None = None
    # Must be an ${ENV_VAR} reference (MORPHYARD_SIGNING_KEY); empty = generated into data_dir.
    signing_key: str | None = Field(None, repr=False)
    ttl_sec: int = Field(3600, gt=0, le=24 * 3600)
    max_total_mb: int = Field(2048, gt=0)
    max_per_owner_mb: int = Field(512, gt=0)
    sweep_interval_sec: float = Field(60, ge=1, le=3600)
    # How tools return files (used from the results-as-links change on):
    #   auto   = link, plus base64 inline when the file is at most inline_max_mb (default)
    #   inline = base64 only, as in 1.3;  both = always link and base64;  link = link only
    result_delivery: Literal["auto", "inline", "both", "link"] = "auto"
    inline_max_mb: float = Field(10, ge=0)

    @field_validator("public_url", "signing_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        return None if v is None or (isinstance(v, str) and not v.strip()) else v

    @field_validator("result_delivery", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class JobsConfig(BaseModel):
    """Conversions as jobs (`jobs:`, issue #34).

    A tool call waits up to `wait_sec` for its conversion; one that takes longer answers with
    a job id, and `job_status` (which waits up to `wait_sec` per call too) returns the result.
    Jobs live in memory: a restart loses them. A finished job is kept as long as files are
    (`files.ttl_sec`).
    """
    # Wait inside a call before answering with a job id, and the longest job_status wait.
    # Keep it below the clients' own timeouts (Claude Code backgrounds a call at ~120 s).
    wait_sec: float = Field(20, gt=0, le=120)
    # Jobs queued or running at once, per person (token `sub`; everyone without auth).
    max_per_owner: int = Field(5, gt=0)
    # Jobs queued or running at once on the whole server.
    max_active: int = Field(64, gt=0)
    # A job is stopped after this long, its wait in the queue included.
    max_sec: float = Field(1800, gt=0)


class FetchConfig(BaseModel):
    """URL inputs (`fetch:`, issue #35): tools can take a file by its http(s) address.

    Only public addresses are fetched; `allow` lists LAN networks (CIDR) and exact host names
    that may be reached anyway, `deny` lists networks and host names that never are (deny wins).
    Both are comma lists in the environment (MORPHYARD_URL_ALLOW / MORPHYARD_URL_DENY).
    """
    enabled: bool = True
    allow: list[str] = []
    deny: list[str] = []
    # The whole fetch, redirects included. Connect (10 s) and idle (15 s) limits are fixed.
    timeout_sec: float = Field(60, gt=0, le=600)

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def _comma_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("allow", "deny")
    @classmethod
    def _valid_entries(cls, v, info):
        from src.domain.fetch.policy import parse_entries
        parse_entries(v, f"MORPHYARD_URL_{info.field_name.upper()}")
        return v


class AppConfig(BaseModel):
    """Application metadata."""
    name: str = "MyMCP"
    # Ignored: the reported version comes from pyproject.toml (src/version.py).
    # Kept so an older config that still sets it loads without error.
    version: str | None = None
    title: str = "My MCP Service"
    description: str = ""


class RouterConfig(BaseModel):
    """API router settings."""
    module: str
    router_name: str
    prefix: str


class McpToolsConfig(BaseModel):
    """MCP tools settings."""
    module: str
    function_name: str


class McpPromptsConfig(BaseModel):
    """MCP prompts settings."""
    module: str
    function_name: str


class McpResourcesConfig(BaseModel):
    """MCP resources settings."""
    module: str
    function_name: str


class ModuleConfig(BaseModel):
    """Settings for a single module."""
    model_config = ConfigDict(extra="allow")

    api_router: RouterConfig | None = None
    mcp_tools: McpToolsConfig | None = None
    mcp_prompts: McpPromptsConfig | None = None
    mcp_resources: McpResourcesConfig | None = None


class ModulesConfig(BaseModel):
    """Top-level modules settings."""
    model_config = ConfigDict(extra="allow")

    enabled: list[str]


class ConfigModel(BaseModel):
    """Complete configuration model.

    To add a config section:
    1. Define its Pydantic model above
    2. Add a field here
    3. Add the matching YAML section to config.yaml
    """
    server: ServerConfig
    auth: AuthConfig = AuthConfig()
    logging: LoggingConfig
    app: AppConfig | None = None
    convert: ConvertConfig = ConvertConfig()
    files: FilesConfig = Field(default_factory=FilesConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    modules: ModulesConfig


__all__ = [
    "AppConfig",
    "AuthConfig",
    "ConfigModel",
    "ConvertConfig",
    "FetchConfig",
    "FilesConfig",
    "JobsConfig",
    "LoggingConfig",
    "McpPromptsConfig",
    "McpResourcesConfig",
    "McpToolsConfig",
    "ModuleConfig",
    "ModulesConfig",
    "RouterConfig",
    "ServerConfig",
]
