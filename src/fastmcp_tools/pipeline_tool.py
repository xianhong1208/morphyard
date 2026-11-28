"""The `pipeline` MCP tool: several Morphyard tools in a row as one call (issue #37).

The domain part (planning, running, fan-out, the ZIP) is src/domain/operations/pipeline.py. This
module is the MCP side: the parameters (the steps, plus the usual single-file parameters or
`files`), the checks made before anything is read, and the job: the whole pipeline is one job
(src/domain/jobs.py), so a long one answers with a job_id after jobs.wait_sec, sends
notifications/progress as "step k of n", and job_cancel stops the step that is running.

Intermediate files stay on the server and never come back to the model: only the last step's
files are returned, kept and linked like any tool's result (operation_tool.deliver_files).
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from src.auth import check_pipeline_steps
from src.domain.jobs import Job
from src.domain.operations import InputSource, pipeline
from src.fastmcp_tools.file_inputs import MAX_FILES_PER_CALL, FileInput, item_sources
from src.fastmcp_tools.operation_tool import (
    _REPLIES,
    JOB_HINT,
    Reply,
    _upload_sources,
    current_owner,
    run_work_as_job,
    upload,
)
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

NAME = "pipeline"
_SINGLE = ("file_content", "file_id", "url")


class PipelineStep(BaseModel):
    """One step: a Morphyard tool and its arguments (without the file)"""

    tool: str = Field(
        description="The tool to run, e.g. 'pdf_merge', 'pdf_compress', 'convert_document'. "
                    f"One of: {', '.join(pipeline.step_tools())}.")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="That tool's own arguments, by name, as you would pass them to the tool "
                    "(e.g. {\"text\": \"DRAFT\"} for pdf_watermark), without file_content, "
                    "file_id, url or files: the step's input is the previous step's result.")


def _given(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sources(kwargs: dict[str, Any], detect: bool, owner: str) -> list[InputSource]:
    """The pipeline's input files, checked before anything is read: `files`, or one file in
    file_content / file_id / url."""
    items = kwargs.pop("files", None)
    single = [name for name in _SINGLE if _given(kwargs.get(name))]
    if items:
        if single:
            raise ToolError(f"Give the input either in files or in {single[0]}, not both: for "
                            "several files put every one of them in files.")
        for name in ("file_content", "file_name", "mime_type", "file_id", "url"):
            kwargs.pop(name, None)
        return item_sources(items, owner, detect)
    if not single:
        raise ToolError("Give the pipeline's input: one file in file_content (an upload "
                        "reference goes there), file_id or url, or several in files. Nothing "
                        "was run.")
    return _upload_sources(kwargs, detect, owner)


DESCRIPTION = """Run several Morphyard tools in a row on the server, as one call, and return only the final file(s).

Use it when the user wants more than one operation on the same file(s), e.g. "merge these,
compress, and stamp DRAFT on it" or "split this PDF and rotate every part", or the same
operation on several files ("compress all of these"). Intermediate results stay on the server
and are not sent back, so this is faster and lighter than calling the tools one by one with
file_id. For a single operation call that tool directly.

Give the input as one file (file_content for an upload reference, file_id or url) or several
in `files`, and the `steps` in order, each {"tool": name, "args": {...}} with that tool's own
arguments (not the file). How files flow: a one-file tool (pdf_compress, pdf_rotate,
convert_document ...) runs on each current file; pdf_split gives several files; pdf_merge and
images_to_pdf combine all current files into one. Every step is checked before anything runs
and an error names the step. At most 10 steps. Several final files come back as links, or as
one ZIP with zip_outputs=true (always one ZIP above 10 files). Tools that answer with data
(extract_text, inspect_document, pdf_metadata) cannot be steps: call them on the result.
"""

STEPS = Annotated[list[PipelineStep], Field(
    min_length=1, max_length=pipeline.MAX_STEPS,
    description="The steps, run in order, e.g. [{\"tool\": \"pdf_merge\", \"args\": {}}, "
                "{\"tool\": \"pdf_compress\", \"args\": {\"level\": \"small\"}}, "
                "{\"tool\": \"pdf_watermark\", \"args\": {\"text\": \"DRAFT\"}}]. "
                f"1 to {pipeline.MAX_STEPS} steps.")]
FILES = Annotated[list[FileInput] | None, Field(
    min_length=1, max_length=MAX_FILES_PER_CALL,
    description="Several input files, in order (pdf_merge keeps this order): each item is a "
                "file's file_id ('f_...', from store_file, create_upload or an earlier "
                "Morphyard tool), the file itself as base64 in file_content plus its "
                "file_name, or a url to download it from. Use instead of file_content / "
                f"file_id / url. 1 to {MAX_FILES_PER_CALL} files.")]
OUTPUT_NAME = Annotated[str | None, Field(
    description="Name without extension for the final file, or for the ZIP when the "
                "results are zipped. Default: the last step's own naming.")]
ZIP_OUTPUTS = Annotated[bool, Field(
    description="Return several final files as one ZIP instead of one link each. More than "
                f"{pipeline.MAX_LINKED_OUTPUTS} files are always zipped.")]

_KW = inspect.Parameter.POSITIONAL_OR_KEYWORD


def register_pipeline_tool(mcp: FastMCP) -> None:
    _REPLIES[NAME] = Reply.FILES
    first, _, rest = inspect.cleandoc(DESCRIPTION).partition("\n\n")
    description = f"{first}\n\n{JOB_HINT}\n\n{rest}"

    params = [
        inspect.Parameter("steps", _KW, annotation=STEPS),
        *upload().params,
        inspect.Parameter("files", _KW, default=None, annotation=FILES),
        inspect.Parameter("output_name", _KW, default=None, annotation=OUTPUT_NAME),
        inspect.Parameter("zip_outputs", _KW, default=False, annotation=ZIP_OUTPUTS),
    ]
    returns = list | str

    async def tool(**kwargs):
        owner = current_owner()
        steps = kwargs.pop("steps")
        files = kwargs.get("files")
        count = len(files) if files else 1
        # Everything that can be checked without reading a file, before the job starts.
        planned = pipeline.plan(steps, count)
        # With MCP Gate's tool policy on, a step is refused when its tool could not be called directly.
        await check_pipeline_steps(s.op.name for s in planned)
        detect = planned[0].op.detect_input_format
        sources = _sources(kwargs, detect, owner)
        output_name, zip_outputs = kwargs.get("output_name"), bool(kwargs.get("zip_outputs"))
        logger.info(f"[pipeline] {len(planned)} step(s) on {count} file(s): "
                    + " -> ".join(s.op.name for s in planned))

        async def work(job: Job):
            return await pipeline.run(planned, sources, output_name=output_name,
                                      zip_outputs=zip_outputs, progress=job.set_progress,
                                      notify=job.say)

        return await run_work_as_job(NAME, Reply.FILES, work, owner, None)

    tool.__name__ = tool.__qualname__ = NAME
    tool.__doc__ = description
    tool.__signature__ = inspect.Signature(params, return_annotation=returns)
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": returns}
    mcp.tool(output_schema=None)(tool)
    logger.info("Pipeline tool registered")


__all__ = ["PipelineStep", "register_pipeline_tool"]
