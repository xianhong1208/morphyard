"""Putting a tool's result files in the store and making their links (issue #33).

`keep_results()` stores every file of one result for its owner and signs a download link for
each. It is all or nothing: when one file does not fit (StoreFullError), the files of that result
already stored are deleted again, so a reply never hands out part of a result.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.files.links import LinkSigner
from src.domain.files.store import LocalStore, StoredFile


@dataclass(frozen=True)
class NewFile:
    """One file to keep: its bytes, file name (with extension) and MIME type."""
    data: bytes
    name: str
    mime: str


@dataclass(frozen=True)
class KeptFile:
    """A stored result file and its signed download link."""
    meta: StoredFile
    url: str


def keep_results(store: LocalStore, signer: LinkSigner, owner: str,
                 files: list[NewFile]) -> list[KeptFile]:
    """Store `files` for `owner` (all of them, or none) and sign a link to each.

    Raises StoreFullError when they do not fit; nothing of this result is left behind.
    """
    kept: list[KeptFile] = []
    try:
        for f in files:
            meta = store.put_bytes(owner, f.data, name=f.name, mime=f.mime, kind="result")
            kept.append(KeptFile(meta=meta, url=signer.url(meta.file_id, meta.name, meta.expires_at)))
    except BaseException:
        for k in kept:
            store.delete(k.meta.file_id)
        raise
    return kept
