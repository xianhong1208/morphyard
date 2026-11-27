"""Pipelines: several operations in a row on the server, as one call (issue #37).

A pipeline is a linear list of steps `{tool, args}`, each naming a registered operation that
produces files. The files flow from step to step and stay in memory: only the last step's
files are returned (by the tool layer, as links). How the files flow follows each operation's
arity:

  one_to_one   runs once per current file (fan-out keeps the count);
  one_to_many  runs once per current file, each run may give several (pdf_split);
  many_to_one  runs once on all current files (pdf_merge, images_to_pdf).

`plan()` checks the whole pipeline before any input is read, and every refusal names the step:
the step count, that each tool exists and produces files, its arguments against the
operation's own model (and its `prepare` checks), the file counts (pdf_merge needs two) and,
where the formats are known, that each step accepts what the previous one produces (a PDF tool
after convert_document to docx is refused). What can only be known while running (the input's
real format, a split's file count) is checked by the operations themselves; such a failure
also names its step.

Nothing here imports MCP. Each operation still takes its conversion slot around its own engine
work, so a long pipeline releases the slot between steps and cannot starve other callers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from src.domain.convert.pdf_images import zip_images
from src.domain.exceptions import DomainException
from src.domain.operations.base import (
    Arity,
    Artifact,
    InputSource,
    Notify,
    Operation,
    OperationRefused,
    OpResult,
    OutputFile,
    execute,
    get_operation,
    list_operations,
)

MAX_STEPS = 10
# Files alive between two steps (a split of several files can multiply them).
MAX_FILES = 200
# More final files than this come back as one ZIP even without zip_outputs.
MAX_LINKED_OUTPUTS = 10

INPUT_ARGS = frozenset({"file_content", "file_name", "mime_type", "file_id", "url", "files"})


class PipelineError(DomainException):
    """A pipeline refused before it ran (error_code pipeline_invalid), or a step that failed
    while it ran (pipeline_step_failed). The message names the step."""


def _invalid(message: str) -> PipelineError:
    return PipelineError(message + " Nothing was run.", "pipeline_invalid")


def step_tools() -> list[str]:
    """The operations a step may name: the ones that produce files."""
    return [op.name for op in list_operations() if op.arity.produces_files]


@dataclass(frozen=True)
class PlannedStep:
    number: int          # 1-based
    op: Operation
    args: BaseModel


def _label(number: int, total: int, tool: str) -> str:
    return f"Step {number} of {total} ({tool})"


def _arg_errors(error: ValidationError) -> str:
    parts = []
    for item in error.errors()[:5]:
        where = ".".join(str(p) for p in item.get("loc", ())) or "args"
        if item.get("type") == "extra_forbidden":
            parts.append(f"'{where}' is not an argument of this tool")
        else:
            parts.append(f"'{where}': {item.get('msg')}")
    return "; ".join(parts)


def plan(steps: Sequence[Any], input_count: int) -> list[PlannedStep]:
    """Check a whole pipeline before anything runs; PipelineError names the failing step.

    `steps` are objects or dicts with `tool` and `args`; `input_count` is how many files the
    pipeline starts with.
    """
    total = len(steps)
    if total == 0:
        raise _invalid("A pipeline needs at least one step.")
    if total > MAX_STEPS:
        raise _invalid(f"A pipeline takes at most {MAX_STEPS} steps; this one has {total}. "
                       "Split it into two pipelines: pass the first one's file_id to the second.")
    allowed = step_tools()
    planned: list[PlannedStep] = []
    count: int | None = input_count    # files between steps; None = several, count unknown
    formats: frozenset[str] | None = None   # formats between steps; None = not known yet
    for number, step in enumerate(steps, start=1):
        tool = step["tool"] if isinstance(step, dict) else step.tool
        raw_args = (step.get("args") if isinstance(step, dict) else step.args) or {}
        tool = (tool or "").strip() if isinstance(tool, str) else tool
        label = _label(number, total, tool)
        if tool not in allowed:
            raise _invalid(f"{label}: '{tool}' is not a tool a pipeline can run. A step may be "
                           f"one of: {', '.join(allowed)}. Tools that answer with data rather "
                           "than a file (extract_text, inspect_document, pdf_metadata, "
                           "list_supported_conversions) go in a separate call on the "
                           "pipeline's result.")
        op = get_operation(tool)
        given_inputs = sorted(INPUT_ARGS & set(raw_args))
        if given_inputs:
            raise _invalid(f"{label}: args gives {', '.join(given_inputs)}, but a step's input "
                           "is the previous step's result (the first step's is the pipeline's "
                           "input). Remove it from args.")
        try:
            args = op.parse_args(raw_args)
        except ValidationError as e:
            raise _invalid(f"{label}: {_arg_errors(e)}.") from None
        try:
            prepared = op.prepare(args) if op.prepare is not None else None
        except DomainException as e:
            raise _invalid(f"{label}: {e.message}") from None

        # File counts
        if op.arity is Arity.MANY_TO_ONE:
            if count is not None and count < op.min_inputs:
                raise _invalid(f"{label} needs at least {op.min_inputs} files, but it would get "
                               f"{count}.")
            count = 1
        elif op.arity is Arity.ONE_TO_MANY:
            count = None

        # Formats
        if formats is not None and op.input_formats is not None \
                and not formats & op.input_formats:
            raise _invalid(f"{label} takes {', '.join(sorted(op.input_formats))}, but the step "
                           f"before it produces {', '.join(sorted(formats))}.")
        if op.outputs_for is not None:
            formats = op.outputs_for(args, prepared)
        else:
            formats = op.output_formats
        planned.append(PlannedStep(number, op, args))
    return planned


Progress = Callable[[float, float, str], Awaitable[None]]


def _as_artifact(f: OutputFile) -> Artifact:
    return Artifact(data=f.data, fmt=f.fmt, name=f.name, file_name=f"{f.name}.{f.fmt}")


def _given(artifact: Artifact) -> InputSource:
    return lambda: artifact


async def _run(step: PlannedStep, total: int, sources: list[InputSource], file_no: int,
               file_count: int, names: list[str], notify: Notify | None) -> OpResult:
    """One run of a step; a failure is re-raised with the step (and file) named."""
    label = _label(step.number, total, step.op.name)
    if file_count > 1:
        label += f", file {file_no} of {file_count}" + (f" ({names[0]})" if names[0] else "")
    try:
        return await execute(step.op, step.args, sources, notify=notify)
    except OperationRefused as refused:
        raise PipelineError(f"{label} failed: {refused.message}{_done_note(step.number)}",
                            "pipeline_step_failed") from None
    except DomainException as e:
        e.message = f"{label} failed: {e.message}{_done_note(step.number)}"
        e.args = (e.message,)
        raise


def _done_note(number: int) -> str:
    if number == 1:
        return " Nothing was kept."
    done = "step 1" if number == 2 else f"steps 1-{number - 1}"
    return f" ({done} had finished; nothing was kept.)"


def _step_line(step: PlannedStep, total: int, results: list[OpResult]) -> str:
    first = results[0].summary.strip()
    line = f"Step {step.number}/{total} {step.op.name}"
    if len(results) > 1:
        line += f" (run on each of {len(results)} files)"
    return f"{line}: {first}"


def _unique_names(files: list[OutputFile]) -> list[str]:
    seen: dict[str, int] = {}
    names = []
    for f in files:
        name = f"{f.name}.{f.fmt}"
        if name in seen:
            seen[name] += 1
            name = f"{f.name}_{seen[name]}.{f.fmt}"
        else:
            seen[name] = 1
        names.append(name)
    return names


async def run(steps: list[PlannedStep], sources: list[InputSource], *,
              output_name: str | None = None, zip_outputs: bool = False,
              progress: Progress | None = None, notify: Notify | None = None) -> OpResult:
    """Run a planned pipeline on its input sources; the last step's files are the result.

    `progress(done, total, message)` is called before each run of a step, with `done` growing
    from 0 to the number of steps (fractions while a step runs over several files).
    `notify` gets the operations' own messages (convert_document's), as in a single call.
    """
    total = len(steps)

    async def say(done: float, message: str) -> None:
        if progress is not None:
            await progress(done, total, message)

    current: list[InputSource] = list(sources)
    names: list[str] = ["" for _ in current]
    lines: list[str] = []
    first_name: str | None = None
    for step in steps:
        label = _label(step.number, total, step.op.name)
        if step.op.arity is Arity.MANY_TO_ONE:
            await say(step.number - 1, f"{label}: {len(current)} file(s)")
            results = [await _run(step, total, current, 1, 1, [""], notify)]
        else:
            results = []
            for i, source in enumerate(current, start=1):
                await say(step.number - 1 + (i - 1) / len(current),
                          f"{label}: file {i} of {len(current)}" if len(current) > 1 else label)
                results.append(await _run(step, total, [source], i, len(current),
                                          [names[i - 1]], notify))
        if first_name is None:
            first_name = results[0].files[0].name if results[0].files else None
        produced = [f for r in results for f in r.files]
        if not produced:
            raise PipelineError(f"{label} produced no file, so the pipeline cannot go on: "
                                f"{results[0].summary}{_done_note(step.number + 1)}",
                                "pipeline_step_failed")
        if len(produced) > MAX_FILES:
            raise PipelineError(
                f"{label} produced {len(produced)} files, more than the {MAX_FILES} a pipeline "
                f"keeps between steps. Use fewer files or pages per run (e.g. a larger "
                f"pages_per_file).{_done_note(step.number + 1)}", "pipeline_step_failed")
        lines.append(_step_line(step, total, results))
        artifacts = [_as_artifact(f) for f in produced]
        current = [_given(a) for a in artifacts]
        names = [a.file_name or "" for a in artifacts]
        final = produced

    name = (output_name or "").strip()
    count_in = len(sources)
    if zip_outputs or len(final) > MAX_LINKED_OUTPUTS:
        stem = name or f"{first_name or 'pipeline'}_results"
        archive = zip_images(list(zip(_unique_names(final), (f.data for f in final))))
        outputs = [OutputFile(archive, "zip", stem)]
        note = f" in one ZIP ({stem}.zip)"
    else:
        outputs = final
        if name and len(final) == 1:
            outputs = [OutputFile(final[0].data, final[0].fmt, name)]
        note = ""
    await say(total, f"Pipeline done: {len(final)} file(s)")
    head = (f"Pipeline done: {total} step(s) on {count_in} file(s) gave {len(final)} "
            f"file(s){note}.")
    return OpResult(summary="\n".join([head, *lines]), files=outputs)


__all__ = ["INPUT_ARGS", "MAX_FILES", "MAX_LINKED_OUTPUTS", "MAX_STEPS", "PipelineError",
           "PlannedStep", "plan", "run", "step_tools"]

