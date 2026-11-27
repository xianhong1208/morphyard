"""Several files in one call: the `files` parameter of pdf_merge and images_to_pdf (issue #36).

Every item is a `FileInput`: the file itself (`file_content`, base64), the `file_id` of a
file kept on the server (a result, a file stored with `store_file`, or an upload made with
`create_upload`), or a `url` to download it from (issue #35). Exactly one of the three.

Checks, in this order, so a bad request is answered before any file is read:
  1. the count (the JSON schema says 1..20 / 2..20; Pydantic refuses the rest);
  2. each item gives exactly one source: the error names the item (1-based) and its name;
then, while the files are read one by one in `execute()`:
  3. any error reading an item (unknown file_id, not base64, too large) names the item;
  4. the files together may not exceed MAX_CALL_MB.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from src.domain.convert.types import UnsupportedFormatError
from src.domain.exceptions import DomainException
from src.domain.operations import Artifact, InputSource
from src.domain.operations.sources import input_source

# Per call, whatever the tool (each file is still capped at MAX_INPUT_MB on its own).
MAX_FILES_PER_CALL = 20
MAX_CALL_MB = 200

STORE_HINT = ("If the host can only put an uploaded file into a single file_content "
              "parameter, first call store_file once per file (it returns a file_id), then "
              "pass the file_ids here.")


class FileInput(BaseModel):
    """One file: the file itself, the file_id of a file kept on the server, or a URL"""

    file_content: str | None = Field(
        default=None,
        description="The file itself, base64-encoded. Leave empty when file_id or url is "
                    "given.")
    file_name: str | None = Field(
        default=None,
        description="Its file name with extension, e.g. 'Chapter 1.pdf' (default: the "
                    "stored name for a file_id).")
    file_id: str | None = Field(
        default=None,
        description="Instead of file_content: the file_id ('f_...') of a file kept on the "
                    "server: a result of an earlier Morphyard tool, a file stored with "
                    "store_file, or an upload made with create_upload. Give only one of "
                    "file_content, file_id and url.")
    url: str | None = Field(
        default=None,
        description="Instead of file_content: an http or https address to download the file "
                    "from (public addresses only). Give only one of file_content, file_id "
                    "and url.")


def _given(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _label(position: int, item: FileInput) -> str:
    name = (item.file_name or "").strip()
    return f"File #{position} in files" + (f" ({name[:80]})" if name else "")


def check_items(items: list[FileInput]) -> None:
    """Refuse an item that gives no file or two (before anything is read)."""
    for position, item in enumerate(items, start=1):
        given = [name for name, value in (("file_content", item.file_content),
                                          ("file_id", item.file_id), ("url", item.url))
                 if _given(value)]
        if len(given) > 1:
            raise ToolError(f"{_label(position, item)} gives "
                            f"{'both ' if len(given) == 2 else ''}{' and '.join(given)}: "
                            "give exactly one of file_content, file_id and url.")
        if not given:
            raise ToolError(f"{_label(position, item)} gives no file: set file_content (the "
                            "file itself, base64), file_id (a file kept on the server) or url "
                            "(an http or https address). " + STORE_HINT)


def _named(position: int, item: FileInput, source: InputSource) -> InputSource:
    """The source, with the item named in any error it raises."""

    async def resolve() -> Artifact:
        try:
            artifact = source()
            if inspect.isawaitable(artifact):  # a URL item downloads
                artifact = await artifact
            return artifact
        except DomainException as e:
            e.message = f"{_label(position, item)}: {e.message}"
            e.args = (e.message,)
            raise
    return resolve


def item_sources(items: list[FileInput], owner: str, detect: bool) -> list[InputSource]:
    """One lazily read source per item, in order, with the per-call size cap."""
    check_items(items)
    total = {"bytes": 0}
    cap = MAX_CALL_MB * 1024 * 1024

    def counted(source: InputSource) -> InputSource:
        async def resolve() -> Artifact:
            artifact = await source()
            total["bytes"] += len(artifact.data)
            if total["bytes"] > cap:
                raise UnsupportedFormatError(
                    f"The files add up to more than {MAX_CALL_MB} MB, the most one call takes. "
                    "Split them over several calls (e.g. merge in batches, then merge the "
                    "results).")
            return artifact
        return resolve

    return [counted(_named(position, item, input_source(
                owner=owner, detect=detect, file_content=item.file_content,
                file_id=item.file_id, file_name=item.file_name, url=item.url)))
            for position, item in enumerate(items, start=1)]


def files_parameter(description: str, *, min_length: int, max_length: int = MAX_FILES_PER_CALL,
                    optional: bool = False) -> inspect.Parameter:
    annotation: Any = list[FileInput]
    if optional:
        annotation = list[FileInput] | None
    return inspect.Parameter(
        "files", inspect.Parameter.POSITIONAL_OR_KEYWORD,
        default=None if optional else inspect.Parameter.empty,
        annotation=Annotated[annotation, Field(
            min_length=min_length, max_length=max_length, description=description)],
    )


__all__ = ["MAX_CALL_MB", "MAX_FILES_PER_CALL", "STORE_HINT", "FileInput", "check_items",
           "files_parameter", "item_sources"]
