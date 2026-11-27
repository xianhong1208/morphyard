"""Signed download links and the /files endpoint (issue #33).

The signature is the only credential, so every way of bending a link (another id, name or
expiry, a missing or truncated signature, a stale file) must end in the same 404. The signing
key and the store are split state: each one missing on its own is tested.
"""

import asyncio
import logging
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api.router.files import _RedactFileQueries, content_disposition
from src.api.router.files import router as files_router
from src.config.model import AuthConfig, ConfigModel, FilesConfig, LoggingConfig, ModulesConfig, ServerConfig
from src.domain import files as files_mod
from src.domain.files import (
    FilesRuntime,
    LinkSigner,
    LocalStore,
    SigningKeyError,
    files_lifespan,
    init_files,
    resolve_public_url,
    set_runtime,
)
from src.domain.files.links import key_fingerprint, load_signing_key

KEY = b"k" * 32


@pytest.fixture(autouse=True)
def _restore_runtime():
    saved = files_mod.get_runtime()
    yield
    set_runtime(saved)


def _runtime(tmp_path, key=KEY, **store_kw):
    store = LocalStore(tmp_path / "store", **store_kw)
    signer = LinkSigner(key, "https://files.example")
    rt = FilesRuntime(store=store, signer=signer, sweep_interval_sec=60,
                      result_delivery="auto", inline_max_bytes=10 * 1024 * 1024)
    set_runtime(rt)
    return rt


def _client():
    app = FastAPI()
    app.include_router(files_router)
    return TestClient(app)


def _path(url):
    parts = urlsplit(url)
    return parts.path + "?" + parts.query


def _cfg(tmp_path, **files_kw):
    files_kw.setdefault("data_dir", str(tmp_path / "data"))
    return ConfigModel(
        server=ServerConfig(host="127.0.0.1", port=5061),
        auth=AuthConfig(enabled=False),
        logging=LoggingConfig(level="WARNING"),
        files=FilesConfig(**files_kw),
        modules=ModulesConfig(enabled=[]),
    )


class TestDownload:
    def test_valid_link_downloads(self, tmp_path):
        """TC-LINK-001: a fresh link returns the bytes with its type, name and safety headers."""
        rt = _runtime(tmp_path)
        meta = rt.store.put_bytes("alice", b"%PDF-1.7 hi", name="report.pdf", mime="application/pdf")
        url = rt.signer.url(meta.file_id, meta.name, meta.expires_at)
        assert url.startswith(f"https://files.example/files/{meta.file_id}/report.pdf?exp=")
        r = _client().get(_path(url))
        assert r.status_code == 200 and r.content == b"%PDF-1.7 hi"
        assert r.headers["content-type"] == "application/pdf"
        assert r.headers["content-disposition"] == "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "private, no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["content-security-policy"] == "sandbox"
        assert _client().get(_path(url)).status_code == 200  # reusable until it expires

    def test_head(self, tmp_path):
        """TC-LINK-002: HEAD answers with the headers and no body."""
        rt = _runtime(tmp_path)
        meta = rt.store.put_bytes("alice", b"12345", name="a.txt", mime="text/plain")
        r = _client().head(_path(rt.signer.url(meta.file_id, meta.name, meta.expires_at)))
        assert r.status_code == 200 and r.content == b"" and r.headers["content-length"] == "5"

    def test_unicode_name(self, tmp_path):
        """TC-LINK-003: a non-ASCII name works in the URL and in Content-Disposition."""
        rt = _runtime(tmp_path)
        meta = rt.store.put_bytes("alice", b"x", name="報告 1.docx", mime="application/octet-stream")
        url = rt.signer.url(meta.file_id, meta.name, meta.expires_at)
        r = _client().get(_path(url))
        assert r.status_code == 200
        assert r.headers["content-disposition"] == (
            "attachment; filename=\"__ 1.docx\"; filename*=UTF-8''%E5%A0%B1%E5%91%8A%201.docx")

    def test_html_is_not_served_inline(self, tmp_path):
        """TC-LINK-004: an HTML result is an attachment under a sandbox CSP, never a live page."""
        rt = _runtime(tmp_path)
        meta = rt.store.put_bytes("alice", b"<script>alert(1)</script>", name="x.html", mime="text/html")
        r = _client().get(_path(rt.signer.url(meta.file_id, meta.name, meta.expires_at)))
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["content-security-policy"] == "sandbox"


class TestRefusals:
    def _setup(self, tmp_path):
        rt = _runtime(tmp_path)
        meta = rt.store.put_bytes("alice", b"secret", name="a.txt", mime="text/plain")
        url = rt.signer.url(meta.file_id, meta.name, meta.expires_at)
        q = parse_qs(urlsplit(url).query)
        return rt, meta, q["exp"][0], q["sig"][0]

    def _get(self, file_id, name, query):
        r = _client().get(f"/files/{file_id}/{name}{query}")
        assert r.status_code == 404, r.text
        assert r.json()["error"] == "not_found" and b"secret" not in r.content
        return r

    def test_tampered_links(self, tmp_path):
        """TC-LINK-010: another expiry, name or id, a changed, truncated or missing signature: 404."""
        rt, meta, exp, sig = self._setup(tmp_path)
        other = rt.store.put_bytes("alice", b"other", name="a.txt", mime="text/plain")
        flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
        cases = [
            (meta.file_id, "a.txt", f"?exp={int(exp) + 3600}&sig={sig}"),
            (meta.file_id, "b.txt", f"?exp={exp}&sig={sig}"),
            (other.file_id, "a.txt", f"?exp={exp}&sig={sig}"),
            (meta.file_id, "a.txt", f"?exp={exp}&sig={flipped}"),
            (meta.file_id, "a.txt", f"?exp={exp}&sig={sig[:-2]}"),
            (meta.file_id, "a.txt", f"?exp={exp}"),
            (meta.file_id, "a.txt", f"?sig={sig}"),
            (meta.file_id, "a.txt", f"?exp=-{exp}&sig={sig}"),
            (meta.file_id, "a.txt", f"?exp={exp}.0&sig={sig}"),
            (meta.file_id, "a.txt", ""),
        ]
        for case in cases:
            self._get(*case)

    def test_other_key_refused(self, tmp_path):
        """TC-LINK-011: a link signed with another key is refused."""
        rt, meta, _, _ = self._setup(tmp_path)
        forged = LinkSigner(b"z" * 32, "https://files.example").url(meta.file_id, meta.name, meta.expires_at)
        self._get(meta.file_id, "a.txt", "?" + urlsplit(forged).query)

    def test_expired_link(self, tmp_path):
        """TC-LINK-012: a link whose time has passed is refused with the same 404 as an unknown one."""
        rt, meta, _, _ = self._setup(tmp_path)
        past = int(time.time()) - 1
        url = rt.signer.url(meta.file_id, meta.name, past)
        r = self._get(meta.file_id, "a.txt", "?" + urlsplit(url).query)
        unknown = _client().get("/files/f_AAAAAAAAAAAAAAAAAAAAAA/a.txt?exp=1&sig=x")
        assert r.json() == unknown.json() and unknown.status_code == 404

    def test_file_gone_before_link_expires(self, tmp_path):
        """TC-LINK-013: a valid link to a deleted or store-expired file is refused."""
        rt, meta, exp, sig = self._setup(tmp_path)
        far = rt.store.put_bytes("alice", b"far", name="far.txt", mime="text/plain", ttl_sec=60)
        long_url = rt.signer.url(far.file_id, far.name, time.time() + 3600)
        rt.store._index[far.file_id] = far.__class__(**{**far.__dict__, "expires_at": time.time() - 1})
        self._get(far.file_id, "far.txt", "?" + urlsplit(long_url).query)
        rt.store.delete(meta.file_id)
        self._get(meta.file_id, "a.txt", f"?exp={exp}&sig={sig}")

    def test_traversal_ids(self, tmp_path):
        """TC-LINK-014: ids that try to leave the store are refused, even with a valid signature."""
        rt, *_ = self._setup(tmp_path)
        (tmp_path / "outside.txt").write_text("secret")
        for bad in ("..", "..%2F..%2Foutside.txt", "%2E%2E", "f_..%2F..%2F..%2Foutside"):
            exp = int(time.time()) + 60
            sig = rt.signer.signature(bad, exp, "outside.txt")
            r = _client().get(f"/files/{bad}/outside.txt?exp={exp}&sig={sig}")
            assert r.status_code == 404 and b"secret" not in r.content

    def test_link_for_another_name(self, tmp_path):
        """TC-LINK-016: a correctly signed link naming the file differently than stored is refused."""
        rt, meta, _, _ = self._setup(tmp_path)
        url = rt.signer.url(meta.file_id, "renamed.txt", meta.expires_at)
        self._get(meta.file_id, "renamed.txt", "?" + urlsplit(url).query)

    def test_no_runtime(self, tmp_path):
        """TC-LINK-015: without a store every download is 404."""
        set_runtime(None)
        assert _client().get("/files/f_AAAAAAAAAAAAAAAAAAAAAA/a.txt?exp=9999999999&sig=x").status_code == 404


class TestSigner:
    def test_signature_binds_every_field(self):
        """TC-LINK-020: id, expiry and name all change the signature."""
        s = LinkSigner(KEY, "http://x")
        base = s.signature("f_a", 100, "a")
        assert len({base, s.signature("f_b", 100, "a"), s.signature("f_a", 101, "a"),
                    s.signature("f_a", 100, "b")}) == 4
        assert s.verify("f_a", "a", "100", base, now=99)
        assert not s.verify("f_a", "a", "100", base, now=100)
        assert not s.verify("f_a", "a", "100", base + "x", now=0)
        assert not s.verify("f_a", "a", None, base, now=0)
        assert not s.verify("f_a", "a", "9" * 13, base, now=0)

    def test_short_key_refused(self):
        """TC-LINK-021: a signer never runs on a key shorter than 32 bytes."""
        with pytest.raises(ValueError):
            LinkSigner(b"short", "http://x")

    def test_content_disposition_fallback(self):
        """TC-LINK-022: quotes, backslashes and percent signs never reach the quoted fallback."""
        assert content_disposition('a"b\\c%d.txt').startswith('attachment; filename="a_b_c_d.txt";')


class TestSigningKey:
    def test_generated_once_with_mode_600(self, tmp_path):
        """TC-LINK-030: without MORPHYARD_SIGNING_KEY a key is generated once (mode 600) and reused."""
        key, source = load_signing_key(tmp_path, None)
        path = tmp_path / ".signing_key"
        assert source == str(path) and len(key) == 32
        assert oct(path.stat().st_mode & 0o777) == oct(0o600)
        assert load_signing_key(tmp_path, None)[0] == key

    def test_env_key(self, tmp_path):
        """TC-LINK-031: MORPHYARD_SIGNING_KEY wins and writes no key file; a short one is refused."""
        key, source = load_signing_key(tmp_path, "s" * 40)
        assert key == b"s" * 40 and source == "MORPHYARD_SIGNING_KEY"
        assert not (tmp_path / ".signing_key").exists()
        with pytest.raises(SigningKeyError):
            load_signing_key(tmp_path, "s" * 31)

    @pytest.mark.parametrize("content", ["not hex\n", "abcd\n", ""])
    def test_corrupt_key_file_refused(self, tmp_path, content):
        """TC-LINK-032: an unreadable or short key file stops the start and is not overwritten."""
        (tmp_path / ".signing_key").write_text(content)
        with pytest.raises(SigningKeyError):
            load_signing_key(tmp_path, None)
        assert (tmp_path / ".signing_key").read_text() == content

    def test_literal_key_in_config_refused(self, tmp_path):
        """TC-LINK-033: files.signing_key written literally in config.yaml stops the start."""
        cfg = _cfg(tmp_path, signing_key="x" * 40)
        with pytest.raises(SigningKeyError):
            init_files(cfg, env_refs={})
        init_files(cfg, env_refs={"files.signing_key": "MORPHYARD_SIGNING_KEY"})


class TestSplitState:
    """The key and the store live side by side; each may go missing on its own."""

    def _start(self, tmp_path, **kw):
        return init_files(_cfg(tmp_path, **kw))

    def test_restart_keeps_files_and_links(self, tmp_path):
        """TC-LINK-040: same key, same store: unexpired files and their old links keep working."""
        rt = self._start(tmp_path)
        meta = rt.store.put_bytes("alice", b"keep", name="k.txt", mime="text/plain")
        url = rt.signer.url(meta.file_id, meta.name, meta.expires_at)
        rt2 = self._start(tmp_path)
        assert rt2.store.get(meta.file_id) == meta
        assert _client().get(_path(url)).content == b"keep"

    def test_key_file_lost_purges_store(self, tmp_path, caplog_loguru):
        """TC-LINK-041: key file deleted, store still full: files are purged with a warning, new key made."""
        rt = self._start(tmp_path)
        meta = rt.store.put_bytes("alice", b"x", name="x", mime="x/y")
        old_url = rt.signer.url(meta.file_id, meta.name, meta.expires_at)
        (tmp_path / "data" / ".signing_key").unlink()
        rt2 = self._start(tmp_path)
        assert rt2.store.get(meta.file_id) is None
        assert not rt2.store.has_entries_on_disk()
        assert (tmp_path / "data" / ".signing_key").exists()
        assert any("signing key changed" in m for m in caplog_loguru)
        assert _client().get(_path(old_url)).status_code == 404

    def test_key_id_lost_purges_store(self, tmp_path, caplog_loguru):
        """TC-LINK-042: the store's key record is gone: its files cannot be trusted, so they are purged."""
        rt = self._start(tmp_path)
        meta = rt.store.put_bytes("alice", b"x", name="x", mime="x/y")
        (rt.store.root / ".key_id").unlink()
        rt2 = self._start(tmp_path)
        assert rt2.store.get(meta.file_id) is None
        assert any("no record of the key" in m for m in caplog_loguru)
        assert (rt2.store.root / ".key_id").read_text().strip() == key_fingerprint(rt2.signer._key)

    def test_env_key_changed_purges_store(self, tmp_path):
        """TC-LINK-043: MORPHYARD_SIGNING_KEY changed between starts: old files are purged."""
        rt = self._start(tmp_path, signing_key="a" * 40)
        meta = rt.store.put_bytes("alice", b"x", name="x", mime="x/y")
        assert self._start(tmp_path, signing_key="a" * 40).store.get(meta.file_id) == meta
        assert self._start(tmp_path, signing_key="b" * 40).store.get(meta.file_id) is None

    def test_store_lost_key_kept(self, tmp_path):
        """TC-LINK-044: store directory gone, key kept: the server starts empty with the same key."""
        rt = self._start(tmp_path)
        key = rt.signer._key
        import shutil
        shutil.rmtree(rt.store.root)
        rt2 = self._start(tmp_path)
        assert rt2.signer._key == key and rt2.store.stats()["files"] == 0

    def test_empty_store_new_key_no_warning(self, tmp_path, caplog_loguru):
        """TC-LINK-045: a first start (nothing stored) creates the key without a purge warning."""
        self._start(tmp_path)
        assert not any("can no longer be verified" in m for m in caplog_loguru)


class TestConfigAndHealth:
    def test_public_url_resolution(self, tmp_path):
        """TC-LINK-050: files.public_url, else auth.base_url, else http://host:port."""
        cfg = _cfg(tmp_path)
        assert resolve_public_url(cfg) == "http://127.0.0.1:5061"
        cfg.auth.base_url = "http://mcp.internal:5055/"
        assert resolve_public_url(cfg) == "http://mcp.internal:5055"
        cfg.files.public_url = "https://files.example.org/"
        assert resolve_public_url(cfg) == "https://files.example.org"
        rt = init_files(cfg)
        assert rt.signer.url("f_x", "a", 1).startswith("https://files.example.org/files/f_x/a?")

    def test_settings_validated(self, tmp_path):
        """TC-LINK-051: TTL over 24 h and an unknown delivery mode are refused; blanks mean unset."""
        with pytest.raises(ValueError):
            FilesConfig(ttl_sec=86401)
        with pytest.raises(ValueError):
            FilesConfig(result_delivery="email")
        f = FilesConfig(public_url="  ", signing_key="", result_delivery="LINK", inline_max_mb="2.5")
        assert f.public_url is None and f.signing_key is None
        assert f.result_delivery == "link" and f.inline_max_mb == 2.5
        assert FilesConfig().result_delivery == "auto" and FilesConfig().inline_max_mb == 10

    def test_committed_config(self, monkeypatch):
        """TC-LINK-052: config/config.yaml reads every files setting from MORPHYARD_* variables."""
        from src.config.config_manager import Config
        saved = dict(vars(Config))
        for var, value in {"MORPHYARD_PUBLIC_URL": "https://dl.example", "MORPHYARD_FILE_TTL_SEC": "600",
                           "MORPHYARD_STORE_MAX_MB": "100", "MORPHYARD_STORE_OWNER_MAX_MB": "10",
                           "MORPHYARD_RESULT_DELIVERY": "both", "MORPHYARD_INLINE_MAX_MB": "1",
                           "MORPHYARD_DATA_DIR": "/srv/morphyard-data",
                           "MORPHYARD_SIGNING_KEY": "q" * 40}.items():
            monkeypatch.setenv(var, value)
        Config.set_config("config/config.yaml")
        f = Config.get_config_model().files
        assert (f.public_url, f.ttl_sec, f.max_total_mb, f.max_per_owner_mb, f.result_delivery,
                f.inline_max_mb, f.data_dir, f.signing_key) == (
            "https://dl.example", 600, 100, 10, "both", 1, "/srv/morphyard-data", "q" * 40)
        assert Config.get_env_refs()["files.signing_key"] == "MORPHYARD_SIGNING_KEY"
        for name in ("_config", "_config_model", "_module_configs", "_env_refs"):
            if name in saved:
                setattr(Config, name, saved[name])

    def test_health_store_block(self, tmp_path):
        """TC-LINK-053: /health reports the store's counts and caps, never file names."""
        from app import create_app
        cfg = _cfg(tmp_path)
        with TestClient(create_app(cfg, transport="http")) as c:
            files_mod.get_store().put_bytes("alice", b"abc", name="secret-plan.pdf", mime="x/y")
            body = c.get("/health").json()
        assert body["store"] == {"enabled": True, "files": 1, "bytes": 3, "owners": 1,
                                 "max_bytes": 2048 * 1024 * 1024,
                                 "max_bytes_per_owner": 512 * 1024 * 1024, "ttl_sec": 3600}
        assert "secret-plan" not in str(body)

    def test_app_serves_downloads(self, tmp_path):
        """TC-LINK-054: the real app routes /files/... to the download endpoint, not to the MCP mount."""
        from app import create_app
        with TestClient(create_app(_cfg(tmp_path), transport="http")) as c:
            meta = files_mod.get_store().put_bytes("alice", b"data", name="d.bin", mime="application/octet-stream")
            url = files_mod.get_signer().url(meta.file_id, meta.name, meta.expires_at)
            assert c.get(_path(url)).content == b"data"
            assert c.get(_path(url).replace("sig=", "sig=x")).status_code == 404


class TestSweeper:
    async def test_lifespan_sweeps_at_start_and_periodically(self, tmp_path):
        """TC-LINK-060: the lifespan sweeps at start and then every interval."""
        rt = _runtime(tmp_path)
        set_runtime(FilesRuntime(store=rt.store, signer=rt.signer, sweep_interval_sec=0.05,
                                 result_delivery="auto", inline_max_bytes=0))
        a = rt.store.put_bytes("alice", b"a", name="a", mime="x/y")
        b = rt.store.put_bytes("alice", b"b", name="b", mime="x/y")
        rt.store._index[a.file_id] = a.__class__(**{**a.__dict__, "expires_at": time.time() - 1})
        async with files_lifespan():
            assert a.file_id not in rt.store._index  # swept at start
            rt.store._index[b.file_id] = b.__class__(**{**b.__dict__, "expires_at": time.time() - 1})
            for _ in range(40):
                await asyncio.sleep(0.05)
                if b.file_id not in rt.store._index:
                    break
            assert b.file_id not in rt.store._index
            assert not (rt.store.root / b.owner_key / b.file_id).exists()


class TestLogRedaction:
    def test_access_log_drops_signature(self):
        """TC-LINK-070: uvicorn's access log line for /files/ carries no query string; others are untouched."""
        f = _RedactFileQueries()
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                ("1.2.3.4:5", "GET", "/files/f_x/a.pdf?exp=1&sig=SECRET", "1.1", 200), None)
        assert f.filter(rec) and "SECRET" not in rec.getMessage() and "/files/f_x/a.pdf" in rec.getMessage()
        rec2 = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("1.2.3.4:5", "GET", "/health?x=1", "1.1", 200), None)
        f.filter(rec2)
        assert "/health?x=1" in rec2.getMessage()
