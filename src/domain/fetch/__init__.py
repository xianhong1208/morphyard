"""URL inputs: fetching a file from an http(s) address with an SSRF guard (issue #35).

`init_fetch(config)` reads `config.fetch` once at app start; `fetch_input(url)` fetches with
those settings and the current input size limit. Without `init_fetch` (tools built alone in
tests) the defaults apply: on, public addresses only.
"""

from __future__ import annotations

from src.domain.fetch.fetcher import (
    FetchSettings,
    Fetched,
    PinnedBackend,
    fetch_url,
    parse_target,
    redirect_target,
    system_resolver,
)
from src.domain.fetch.policy import AddressPolicy, UrlInputError, UrlRefusedError, address_problem
from src.log import get_server_logger

logger = get_server_logger()

_settings = FetchSettings()


def init_fetch(config) -> FetchSettings:
    """Build the settings from `config.fetch` (raises ValueError on a bad allow/deny entry)."""
    global _settings
    fetch = getattr(config, "fetch", None)
    if fetch is None:
        _settings = FetchSettings()
        return _settings
    _settings = FetchSettings(
        enabled=fetch.enabled,
        policy=AddressPolicy.from_lists(fetch.allow, fetch.deny),
        total_timeout_sec=fetch.timeout_sec,
    )
    if _settings.enabled:
        logger.info(f"URL input: on, public addresses only unless allowed "
                    f"({_settings.policy.describe()}); {fetch.timeout_sec:g}s per fetch")
    else:
        logger.info("URL input: off (MORPHYARD_URL_FETCH=false)")
    return _settings


def get_fetch_settings() -> FetchSettings:
    return _settings


def set_fetch_settings(settings: FetchSettings) -> None:
    global _settings
    _settings = settings


async def fetch_input(url: str) -> Fetched:
    """The file at `url` with the app's settings, capped at the input size limit."""
    from src.domain.convert import service

    return await fetch_url(url, settings=_settings, max_bytes=service.MAX_INPUT_MB * 1024 * 1024)


__all__ = [
    "AddressPolicy", "FetchSettings", "Fetched", "PinnedBackend", "UrlInputError",
    "UrlRefusedError", "address_problem", "fetch_input", "fetch_url", "get_fetch_settings",
    "init_fetch", "parse_target", "redirect_target", "set_fetch_settings", "system_resolver",
]
