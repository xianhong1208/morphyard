"""Shared test setup."""

import os
import shutil
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _private_temp_dir(tmp_path_factory):
    """Run the whole suite against a temp directory of its own.

    Starting the app sweeps stale LibreOffice profiles from the temp directory, and
    the real conversions write profiles and workspaces there. Neither may touch the
    machine's real /tmp, where a server running next to the tests keeps its own.
    """
    saved = tempfile.tempdir
    tempfile.tempdir = str(tmp_path_factory.mktemp("tmp"))
    yield
    tempfile.tempdir = saved


_created_data_dir: str | None = None


def pytest_configure(config):
    """Apps built by the tests keep their file store and signing key in a temp directory,
    never in the checkout's ./data. Set before any test module is imported."""
    global _created_data_dir
    if not os.environ.get("MORPHYARD_DATA_DIR"):
        _created_data_dir = tempfile.mkdtemp(prefix="morphyard-test-data-")
        os.environ["MORPHYARD_DATA_DIR"] = _created_data_dir


def pytest_unconfigure(config):
    if _created_data_dir:
        shutil.rmtree(_created_data_dir, ignore_errors=True)
        os.environ.pop("MORPHYARD_DATA_DIR", None)


@pytest.fixture
def caplog_loguru():
    """Messages logged through loguru while the test runs."""
    from loguru import logger

    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m.record["message"]), level="DEBUG")
    yield messages
    logger.remove(handler_id)


@pytest.fixture(autouse=True)
def _no_file_store_left_over():
    """Each test starts without a file store runtime (tools then reply as in 1.3, `inline`);
    a test that builds an app or sets a runtime does not leak it into the next test."""
    from src.domain import files

    saved = files.get_runtime()
    files.set_runtime(None)
    yield
    files.set_runtime(saved)
