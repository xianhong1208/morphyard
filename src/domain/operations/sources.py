"""Where an operation's input comes from: lazy `InputSource`s (issue #33).

An input is either the file itself (`file_content`: base64 or plain text, as every tool has
always taken it) or the `file_id` of a file kept on the server, such as the result of an earlier
tool. `input_source()` picks the one the caller gave; nothing is read until `execute()` calls the
source, so a bad argument is still answered before any input is touched.

A `file_id` is owner-checked: only the person whose call stored the file can use it. An id that
is unknown, expired or someone else's gets the same refusal, so the answer never says whether a
file exists.
"""

from __future__ import annotations

from src.domain.convert.service import (
    NO_FILE_MESSAGE,
    check_raw_input,
    read_raw_upload,
    resolve_artifact_input,
    resolve_stored_input,
)
from src.domain.convert.types import UnsupportedFormatError, stem_of
from src.domain.exceptions import DomainException
from src.domain.files import get_store, valid_file_id
from src.domain.operations.base import Artifact, InputSource

# The owner of everything stored while authentication is off: every caller is the same person.
ANONYMOUS_OWNER = "anonymous"

BOTH_GIVEN_MESSAGE = (
    "Give either file_content or file_id, not both: file_content is the file itself, "
    "file_id names a file an earlier Morphyard tool returned."
)


class UnknownFileError(DomainException):
    """A `file_id` that is unknown, expired or not the caller's (one message for all three)."""

    def __init__(self, file_id: str):
        shown = file_id if valid_file_id(file_id) else repr(file_id[:40])
        super().__init__(
            f"File {shown} is unknown, has expired, or was made by someone else. Files are "
            "kept for a limited time after the tool that made them returns. Run that tool "
            "again to get a new file_id, or pass the file itself in file_content. Retrying "
            "with the same file_id will not succeed.",
            "file_not_found",
        )


def _given(value: object) -> str | None:
    """A parameter value that counts as given ("" and whitespace do not: a host blanks the
    parameter it took an upload reference from)."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def upload_source(file_content: str | None, file_name: str | None,
                  mime_type: str | None, detect: bool = True) -> InputSource:
    """The file itself, base64 or plain text."""

    def resolve() -> Artifact:
        if not detect:
            raw = read_raw_upload(file_content, file_name)
            return Artifact(data=raw, fmt=None, name=stem_of(file_name), file_name=file_name)
        source = resolve_artifact_input(file_content, file_name, mime_type)
        return Artifact(data=source.data, fmt=source.fmt, name=source.name, file_name=file_name)
    return resolve


def read_stored(file_id: str, owner: str) -> tuple[bytes, str, str]:
    """(bytes, stored name, stored MIME type) of the caller's live file, or UnknownFileError."""
    store = get_store()
    meta = store.get(file_id, owner) if store is not None else None
    if meta is None:
        raise UnknownFileError(file_id)
    try:
        with store.open(file_id) as f:
            data = f.read()
    except FileNotFoundError:  # expired or deleted between the lookup and the read
        raise UnknownFileError(file_id) from None
    return data, meta.name, meta.mime


def stored_source(file_id: str, owner: str, file_name: str | None = None,
                  mime_type: str | None = None, detect: bool = True) -> InputSource:
    """A file kept on the server, by its id. `file_name` overrides the stored name."""

    def resolve() -> Artifact:
        data, stored_name, stored_mime = read_stored(file_id.strip(), owner)
        name = _given(file_name) or stored_name
        if not detect:
            check_raw_input(data, name)
            return Artifact(data=data, fmt=None, name=stem_of(name), file_name=name)
        source = resolve_stored_input(data, name, _given(mime_type) or stored_mime)
        return Artifact(data=source.data, fmt=source.fmt, name=source.name, file_name=name)
    return resolve


def input_source(*, owner: str, file_content: str | None = None, file_id: str | None = None,
                 file_name: str | None = None, mime_type: str | None = None,
                 detect: bool = True) -> InputSource:
    """The source for one input given as file_content or file_id (exactly one of them)."""
    content, fid = _given(file_content), _given(file_id)
    if content is not None and fid is not None:
        def both() -> Artifact:
            raise UnsupportedFormatError(BOTH_GIVEN_MESSAGE)
        return both
    if fid is not None:
        return stored_source(fid, owner, file_name, mime_type, detect)
    if content is None:
        def neither() -> Artifact:
            raise UnsupportedFormatError(NO_FILE_MESSAGE, file_name=file_name)
        return neither
    return upload_source(file_content, file_name, mime_type, detect)


__all__ = [
    "ANONYMOUS_OWNER", "BOTH_GIVEN_MESSAGE", "UnknownFileError", "input_source",
    "read_stored", "stored_source", "upload_source",
]
