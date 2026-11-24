"""The operations registry: what every Morphyard tool does, independent of MCP.

One `Operation` per tool. It declares

  - `name`         the tool name it backs (also the pipeline step name later);
  - `args_model`   a Pydantic model of the tool's own arguments, with the same
                   names, types, defaults and descriptions as the MCP tool (the
                   tool's input parameters, the uploaded file(s), are not in it);
  - `arity`        how many input files it takes and what it gives back;
  - `input_formats` / `output_formats`  the formats it accepts and produces;
  - `prepare`      checks of the arguments alone, run before any input is read,
                   so a bad option is answered at once (and, for the pipeline,
                   before any step runs);
  - `run`          the work: resolved inputs + validated arguments -> `OpResult`.

`execute()` is the one path that runs an operation: prepare, resolve and check
each input in turn, then run. The MCP tools (src/fastmcp_tools/) are thin
wrappers around it, and so will be jobs and pipeline steps. Nothing here
imports MCP: replies are shaped by the tool layer.

Operations take the conversion slot (limiter.py) themselves, around the engine
work only, exactly where the tools took it before; `takes_slot` records which do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from src.domain.exceptions import DomainException


class Arity(StrEnum):
    """Inputs -> outputs of an operation.

    The first three produce files; the `*_to_report` ones answer with data
    (a dict or text) and produce no file, so a pipeline cannot chain them.
    """

    MANY_TO_ONE = "many_to_one"        # pdf_merge
    ONE_TO_ONE = "one_to_one"          # most operations
    ONE_TO_MANY = "one_to_many"        # pdf_split
    ONE_TO_REPORT = "one_to_report"    # inspect_document, extract_text, pdf_metadata
    NONE_TO_REPORT = "none_to_report"  # list_supported_conversions

    @property
    def inputs(self) -> str:
        """'none', 'one' or 'many'"""
        return self.value.split("_to_")[0]

    @property
    def produces_files(self) -> bool:
        return not self.value.endswith("_report")


@dataclass(frozen=True)
class Artifact:
    """One resolved input file."""

    data: bytes
    fmt: str | None      # detected format; None when the operation reads raw bytes
    name: str            # stem used to name outputs
    file_name: str | None = None  # the name exactly as the caller gave it


@dataclass(frozen=True)
class OutputFile:
    """One file an operation produced."""

    data: bytes
    fmt: str
    name: str            # without extension


@dataclass
class OpResult:
    """What an operation returns.

    summary  the one-paragraph text the tool shows next to its files;
    files    the produced files (empty for a report, or when nothing was done);
    report   the data a report operation answers with (dict or str).
    """

    summary: str = ""
    files: list[OutputFile] = field(default_factory=list)
    report: Any = None


Notify = Callable[[str], Awaitable[None]]


@dataclass
class OpCall:
    """Everything `run` gets: inputs, validated arguments, what `prepare` built,
    and an optional progress callback (MCP log notifications today)."""

    inputs: list[Artifact]
    args: BaseModel
    prepared: Any = None
    notify: Notify | None = None

    async def say(self, message: str) -> None:
        if self.notify is not None:
            await self.notify(message)


class OperationRefused(DomainException):
    """A request the operation declines with a plain answer rather than an error
    (e.g. "This is not a PDF"). The tool layer returns the message as its reply,
    in the tool's usual shape; a pipeline step treats it as a failed step."""

    def __init__(self, message: str):
        super().__init__(message, "operation_refused")


@dataclass(frozen=True)
class Operation:
    name: str
    title: str
    args_model: type[BaseModel]
    arity: Arity
    run: Callable[[OpCall], Awaitable[OpResult]]
    # Formats accepted / produced. None for inputs = any format the service can
    # detect; None for outputs = chosen by the arguments (convert_document).
    input_formats: frozenset[str] | None = None
    output_formats: frozenset[str] | None = None
    prepare: Callable[[BaseModel], Any] | None = None
    # Message for an input whose format is not accepted, given the artifact and
    # its 1-based position. Required when input_formats is set.
    wrong_input: Callable[[Artifact, int], str] | None = None
    # Message sent through `notify` before anything else (convert_document).
    start_message: Callable[[BaseModel], str] | None = None
    # False when the input is read as raw bytes without format detection
    # (images_to_pdf takes a ZIP of images, which is not a document format).
    detect_input_format: bool = True
    takes_slot: bool = True

    def parse_args(self, values: dict[str, Any] | BaseModel) -> BaseModel:
        """Validate arguments against this operation's model."""
        if isinstance(values, self.args_model):
            return values
        if isinstance(values, BaseModel):
            values = values.model_dump()
        return self.args_model.model_validate(values)

    def check_input(self, artifact: Artifact, position: int) -> str | None:
        """The refusal for an input this operation does not accept, or None."""
        if self.input_formats is None or artifact.fmt is None:
            return None
        if artifact.fmt in self.input_formats:
            return None
        return self.wrong_input(artifact, position)  # type: ignore[misc]

    def describe(self) -> dict[str, Any]:
        """A JSON-ready description (for listings and pipeline validation)."""
        return {
            "name": self.name,
            "title": self.title,
            "arity": self.arity.value,
            "input_formats": sorted(self.input_formats) if self.input_formats else None,
            "output_formats": sorted(self.output_formats) if self.output_formats else None,
            "takes_slot": self.takes_slot,
            "args_schema": self.args_model.model_json_schema(),
        }


_REGISTRY: dict[str, Operation] = {}


def register(op: Operation) -> Operation:
    if op.name in _REGISTRY:
        raise ValueError(f"operation {op.name!r} is registered twice")
    if op.input_formats is not None and op.wrong_input is None:
        raise ValueError(f"operation {op.name!r} restricts input formats without a message")
    _REGISTRY[op.name] = op
    return op


def get_operation(name: str) -> Operation:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown operation {name!r}") from None


def list_operations() -> list[Operation]:
    """Every registered operation, in registration order."""
    return list(_REGISTRY.values())


InputSource = Callable[[], Artifact]


def _check_count(op: Operation, count: int) -> None:
    wanted = op.arity.inputs
    ok = (wanted == "none" and count == 0) or (wanted == "one" and count == 1) \
        or (wanted == "many" and count >= 1)
    if not ok:
        raise ValueError(f"operation {op.name!r} ({op.arity.value}) was given {count} input(s)")


async def execute(
    op: Operation | str,
    args: BaseModel | dict[str, Any],
    sources: Sequence[InputSource] = (),
    notify: Notify | None = None,
) -> OpResult:
    """Run one operation.

    Order (it decides which error a bad request gets, so it is fixed):
      1. the start message, if the operation has one;
      2. `prepare` (checks of the arguments alone);
      3. each input resolved and checked in turn: the first input that cannot
         be read raises, the first of a wrong format raises OperationRefused;
      4. `run`.

    `sources` are callables so inputs are read lazily, one at a time, in this
    order; where the bytes come from (base64 today, a stored file or a URL later)
    is the caller's business.
    """
    if isinstance(op, str):
        op = get_operation(op)
    args = op.parse_args(args)
    _check_count(op, len(sources))
    if op.start_message is not None and notify is not None:
        await notify(op.start_message(args))
    prepared = op.prepare(args) if op.prepare is not None else None
    inputs: list[Artifact] = []
    for position, source in enumerate(sources, start=1):
        artifact = source()
        refusal = op.check_input(artifact, position)
        if refusal is not None:
            raise OperationRefused(refusal)
        inputs.append(artifact)
    return await op.run(OpCall(inputs=inputs, args=args, prepared=prepared, notify=notify))
