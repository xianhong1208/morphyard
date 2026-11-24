"""The result store: files kept on the server for a short time (issue #33).

Pins the rules the download links and later tools rely on: owner binding, expiry, caps that
refuse instead of evicting unexpired files, atomic writes, the startup scan, and ids and names
that can never point outside the store.
"""

import hashlib
import json
import os
import threading
import time

import pytest

from src.domain.files.store import (
    MAX_TTL_SEC,
    LocalStore,
    StoreFullError,
    owner_key,
    safe_mime,
    safe_name,
    valid_file_id,
)

KB = 1024


def _store(tmp_path, **kw):
    kw.setdefault("max_total_bytes", 10 * KB)
    kw.setdefault("max_owner_bytes", 4 * KB)
    return LocalStore(tmp_path / "store", **kw)


def _expire(store, file_id):
    """Rewrite a file's expiry into the past, in the index and on disk."""
    meta = store._index[file_id]
    old = meta.__class__(**{**meta.__dict__, "expires_at": time.time() - 1})
    store._index[file_id] = old
    meta_path = store.root / meta.owner_key / file_id / "meta.json"
    raw = json.loads(meta_path.read_text())
    raw["expires_at"] = old.expires_at
    meta_path.write_text(json.dumps(raw))


class TestPutAndGet:
    def test_put_bytes_layout(self, tmp_path):
        """TC-STORE-001: a stored file sits under sha256(sub)/<id>/ with blob + meta; no raw sub on disk."""
        store = _store(tmp_path)
        meta = store.put_bytes("alice@example", b"hello", name="report.pdf", mime="application/pdf")
        assert valid_file_id(meta.file_id)
        assert meta.owner_key == hashlib.sha256(b"alice@example").hexdigest()[:32]
        entry = store.root / meta.owner_key / meta.file_id
        assert (entry / "blob").read_bytes() == b"hello"
        assert json.loads((entry / "meta.json").read_text())["name"] == "report.pdf"
        assert meta.sha256 == hashlib.sha256(b"hello").hexdigest()
        assert meta.size == 5 and meta.mime == "application/pdf" and meta.kind == "result"
        assert all("alice" not in p.name for p in store.root.rglob("*"))
        assert oct(entry.stat().st_mode & 0o777) == oct(0o700)

    def test_owner_binding(self, tmp_path):
        """TC-STORE-002: get/delete with an owner only see that owner's files."""
        store = _store(tmp_path)
        meta = store.put_bytes("alice", b"x", name="a.txt", mime="text/plain")
        assert store.get(meta.file_id, owner="bob") is None
        assert store.delete(meta.file_id, owner="bob") is False
        assert store.get(meta.file_id, owner="alice") == meta
        assert store.get(meta.file_id) == meta
        assert store.delete(meta.file_id, owner="alice") is True
        assert store.get(meta.file_id) is None
        assert not (store.root / meta.owner_key).exists()

    def test_put_file_copies_and_hashes(self, tmp_path):
        """TC-STORE-003: put_file stores a copy of a file on disk, hashed while copied."""
        src = tmp_path / "in.bin"
        src.write_bytes(os.urandom(3000))
        store = _store(tmp_path)
        meta = store.put_file("alice", src, name="in.bin", mime="application/octet-stream")
        assert store.open(meta.file_id).read() == src.read_bytes()
        assert meta.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
        assert src.exists()

    def test_failed_write_leaves_nothing(self, tmp_path, monkeypatch):
        """TC-STORE-004: a write that fails half way leaves no entry, no .tmp and no reserved room."""
        store = _store(tmp_path)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("src.domain.files.store.os.replace", boom)
        with pytest.raises(OSError):
            store.put_bytes("alice", b"x" * 100, name="a", mime="text/plain")
        monkeypatch.undo()
        assert list(store.root.rglob("*.tmp")) == []
        assert store._index == {} and store._reserved_total == 0 and store._reserved_owner == {}
        store.put_bytes("alice", b"x" * 4 * KB, name="a", mime="text/plain")  # the full share is free


class TestExpiry:
    def test_ttl_rules(self, tmp_path):
        """TC-STORE-010: default TTL; a caller may ask for less, never more; the default is at most 24 h."""
        store = _store(tmp_path, default_ttl_sec=600)
        now = time.time()
        assert store.put_bytes("a", b"1", name="a", mime="x/y").expires_at == pytest.approx(now + 600, abs=5)
        assert store.put_bytes("a", b"1", name="a", mime="x/y", ttl_sec=60).expires_at == pytest.approx(now + 60, abs=5)
        assert store.put_bytes("a", b"1", name="a", mime="x/y", ttl_sec=10**6).expires_at == pytest.approx(now + 600, abs=5)
        with pytest.raises(ValueError):
            store.put_bytes("a", b"1", name="a", mime="x/y", ttl_sec=0)
        with pytest.raises(ValueError):
            LocalStore(tmp_path / "s2", default_ttl_sec=MAX_TTL_SEC + 1)

    def test_expired_is_invisible_and_swept(self, tmp_path):
        """TC-STORE-011: an expired file is not served and the sweep deletes it; live files stay."""
        store = _store(tmp_path)
        old = store.put_bytes("alice", b"old", name="old.txt", mime="text/plain")
        live = store.put_bytes("alice", b"live", name="live.txt", mime="text/plain")
        _expire(store, old.file_id)
        assert store.get(old.file_id) is None and store.blob_path(old.file_id) is None
        result = store.sweep()
        assert (result.removed, result.freed_bytes) == (1, 3)
        assert not (store.root / old.owner_key / old.file_id).exists()
        assert store.get(live.file_id) == live

    def test_sweep_never_removes_unexpired(self, tmp_path):
        """TC-STORE-012: a sweep with nothing expired removes nothing."""
        store = _store(tmp_path)
        metas = [store.put_bytes("alice", b"x", name="x", mime="x/y") for _ in range(3)]
        assert store.sweep().removed == 0
        assert all(store.get(m.file_id) for m in metas)


class TestCaps:
    def test_owner_cap_refuses(self, tmp_path):
        """TC-STORE-020: over the per-person cap the write is refused; others still have room."""
        store = _store(tmp_path)
        first = store.put_bytes("alice", b"x" * 3 * KB, name="a", mime="x/y")
        with pytest.raises(StoreFullError) as e:
            store.put_bytes("alice", b"x" * 2 * KB, name="b", mime="x/y")
        assert e.value.error_code == "storage_full"
        assert e.value.details["scope"] == "owner" and e.value.details["retry_after_sec"] >= 1
        assert store.get(first.file_id) == first
        store.put_bytes("bob", b"x" * 2 * KB, name="b", mime="x/y")

    def test_total_cap_refuses(self, tmp_path):
        """TC-STORE-021: over the total cap the write is refused, scope total; nothing is evicted."""
        store = _store(tmp_path, max_total_bytes=5 * KB)
        a = store.put_bytes("alice", b"x" * 3 * KB, name="a", mime="x/y")
        with pytest.raises(StoreFullError) as e:
            store.put_bytes("bob", b"x" * 3 * KB, name="b", mime="x/y")
        assert e.value.details["scope"] == "total"
        assert store.get(a.file_id) == a

    def test_full_store_sweeps_expired_first(self, tmp_path):
        """TC-STORE-022: when full, expired files are swept and the write goes through."""
        store = _store(tmp_path)
        old = store.put_bytes("alice", b"x" * 3 * KB, name="a", mime="x/y")
        _expire(store, old.file_id)
        store.put_bytes("alice", b"x" * 3 * KB, name="b", mime="x/y")
        assert not (store.root / old.owner_key / old.file_id).exists()

    def test_concurrent_writes_respect_cap(self, tmp_path):
        """TC-STORE-023: writes racing for the last room never exceed the cap together."""
        store = _store(tmp_path, max_owner_bytes=5 * 100)
        ok, full = [], []

        def put():
            try:
                ok.append(store.put_bytes("alice", b"x" * 100, name="x", mime="x/y"))
            except StoreFullError:
                full.append(1)

        threads = [threading.Thread(target=put) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(ok) == 5 and len(full) == 7

    def test_retry_after_points_at_next_expiry(self, tmp_path):
        """TC-STORE-024: Retry-After is when enough files expire, not a fixed number."""
        store = _store(tmp_path, default_ttl_sec=3600)
        store.put_bytes("alice", b"x" * 3 * KB, name="a", mime="x/y", ttl_sec=120)
        with pytest.raises(StoreFullError) as e:
            store.put_bytes("alice", b"x" * 2 * KB, name="b", mime="x/y")
        assert 100 <= e.value.details["retry_after_sec"] <= 125


class TestRestart:
    def test_restart_keeps_live_and_drops_the_rest(self, tmp_path):
        """TC-STORE-030: a new store on the same directory keeps live files and removes expired,
        half-written, incomplete and foreign entries."""
        store = _store(tmp_path)
        live = store.put_bytes("alice", b"live", name="live.txt", mime="text/plain")
        old = store.put_bytes("alice", b"old", name="old.txt", mime="text/plain")
        _expire(store, old.file_id)
        no_meta = store.put_bytes("alice", b"m", name="m", mime="x/y")
        (store.root / no_meta.owner_key / no_meta.file_id / "meta.json").unlink()
        no_blob = store.put_bytes("bob", b"b", name="b", mime="x/y")
        (store.root / no_blob.owner_key / no_blob.file_id / "blob").unlink()
        short = store.put_bytes("bob", b"bbbb", name="b", mime="x/y")
        (store.root / short.owner_key / short.file_id / "blob").write_bytes(b"b")
        tmp = store.root / live.owner_key / "f_AAAAAAAAAAAAAAAAAAAAAA.tmp"
        tmp.mkdir()
        (store.root / "not-an-owner").mkdir()
        (store.root / live.owner_key / "stray.txt").write_text("x")

        again = _store(tmp_path)
        assert again.get(live.file_id) == live
        assert set(again._index) == {live.file_id}
        assert not tmp.exists() and not (again.root / "not-an-owner").exists()
        assert not (again.root / live.owner_key / "stray.txt").exists()
        for gone in (old, no_meta, no_blob, short):
            assert not (again.root / gone.owner_key / gone.file_id).exists()
        assert not (again.root / no_blob.owner_key).exists()

    def test_meta_must_match_its_place(self, tmp_path):
        """TC-STORE-031: a meta.json naming another id or owner than its directory is dropped."""
        store = _store(tmp_path)
        meta = store.put_bytes("alice", b"x", name="x", mime="x/y")
        path = store.root / meta.owner_key / meta.file_id / "meta.json"
        raw = json.loads(path.read_text())
        raw["owner_key"] = owner_key("bob")
        path.write_text(json.dumps(raw))
        assert _store(tmp_path).get(meta.file_id) is None

    def test_purge_all_keeps_dot_files(self, tmp_path):
        """TC-STORE-032: purge_all deletes every file but not the store's own records (.key_id)."""
        store = _store(tmp_path)
        (store.root / ".key_id").write_text("abc")
        store.put_bytes("alice", b"x", name="x", mime="x/y")
        assert store.purge_all() == 1
        assert [p.name for p in store.root.iterdir()] == [".key_id"]
        assert not store.has_entries_on_disk()


class TestIdsAndNames:
    @pytest.mark.parametrize("bad", [
        "../../etc/passwd", "/etc/passwd", "f_../../../../etc/passw", "f_" + "A" * 21,
        "f_" + "A" * 23, "g_" + "A" * 22, "f_" + "A" * 21 + "/", "", None, 5,
    ])
    def test_invalid_ids_never_reach_the_disk(self, tmp_path, bad):
        """TC-STORE-040: anything but f_ + 22 URL-safe characters is unknown, whatever is on disk."""
        store = _store(tmp_path)
        assert not valid_file_id(bad)
        assert store.get(bad) is None and store.blob_path(bad) is None and store.delete(bad) is False

    @pytest.mark.parametrize("raw,expected", [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\report.pdf", "report.pdf"),
        ("..", "file"),
        ("  .hidden. ", "hidden"),
        ("a\x00b\r\n.txt", "ab.txt"),
        ('he said "hi"?.md', "he said hi.md"),
        ("報告 2026.docx", "報告 2026.docx"),
        ("rtl\u202etxt.exe", "rtltxt.exe"),
        ("", "file"),
        (None, "file"),
    ])
    def test_safe_name(self, raw, expected):
        """TC-STORE-041: names lose path parts, control/format characters, quotes and edge dots."""
        assert safe_name(raw) == expected

    def test_long_name_keeps_extension(self):
        """TC-STORE-042: a long name is cut to 200 UTF-8 bytes and keeps its extension."""
        out = safe_name("報" * 300 + ".pdf")
        assert out.endswith(".pdf") and len(out.encode("utf-8")) <= 200
        assert len(safe_name("a" * 500).encode()) <= 200

    @pytest.mark.parametrize("raw,expected", [
        ("application/pdf", "application/pdf"),
        ("Text/Plain; charset=utf-8", "text/plain"),
        ("text/html\r\nX-Evil: 1", "application/octet-stream"),
        ("nonsense", "application/octet-stream"),
        (None, "application/octet-stream"),
    ])
    def test_safe_mime(self, raw, expected):
        """TC-STORE-043: only a plain type/subtype is kept as the stored content type."""
        assert safe_mime(raw) == expected

    def test_stored_name_is_sanitised(self, tmp_path):
        """TC-STORE-044: put stores the safe name, not the one given."""
        store = _store(tmp_path)
        assert store.put_bytes("a", b"x", name="../x/../evil.sh", mime="x/y").name == "evil.sh"


class TestStats:
    def test_stats_have_no_names(self, tmp_path):
        """TC-STORE-050: stats count files, bytes and owners, and name nothing."""
        store = _store(tmp_path)
        store.put_bytes("alice", b"abc", name="secret-plan.pdf", mime="x/y")
        store.put_bytes("bob", b"de", name="b", mime="x/y")
        stats = store.stats()
        assert stats == {"files": 2, "bytes": 5, "owners": 2, "max_bytes": 10 * KB,
                         "max_bytes_per_owner": 4 * KB, "ttl_sec": 3600}
        assert "secret" not in json.dumps(stats)
