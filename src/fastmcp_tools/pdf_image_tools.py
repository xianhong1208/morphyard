"""MCP tools: PDF pages to images, images to a PDF

Thin wrappers over the operations in src/domain/operations/pdf_image_ops.py
(which explains why several pages come back as one ZIP). The input triple and
the reply shape are the same as in morphyard_tools.py. images_to_pdf also takes
`files` (file_inputs.py): several images in one call, each as a file_id or base64.
"""

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from src.domain.operations import InputSource
from src.fastmcp_tools.file_inputs import MAX_FILES_PER_CALL, files_parameter, item_sources
from src.fastmcp_tools.operation_tool import (
    Inputs,
    Reply,
    _upload_sources,
    add_operation_tool,
    upload,
)
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

_SINGLE = ("file_content", "file_id")


def _image_sources(kwargs: dict[str, Any], detect: bool, owner: str) -> list[InputSource]:
    """`files` when given (and then nothing in the single-file parameters), else the one file."""
    items = kwargs.pop("files", None)
    if not items:
        return _upload_sources(kwargs, detect, owner)
    given = [name for name in _SINGLE
             if isinstance(kwargs.get(name), str) and kwargs[name].strip()]
    if given:
        raise ToolError(
            f"Give the images either in files or in {given[0]}, not both: for several images "
            "put every one of them in files.")
    for name in ("file_content", "file_name", "mime_type", "file_id"):
        kwargs.pop(name, None)
    return item_sources(items, owner, detect)


IMAGE_FILES = Inputs(
    params=upload().params + [files_parameter(
        "Several images, one page each, in the order given: each item is an image's "
        "file_id ('f_...', from store_file, create_upload or an earlier Morphyard tool), or "
        "the image itself as base64 in file_content plus its file_name. Use instead of "
        f"file_content / file_id. 1 to {MAX_FILES_PER_CALL} images.",
        min_length=1, optional=True)],
    sources=_image_sources,
)


def register_pdf_image_tools(mcp: FastMCP):
    """Register pdf_to_images and images_to_pdf."""

    add_operation_tool(mcp, "pdf_to_images", """Render PDF pages as images, one image per page.

        One page comes back as a single image file; several pages come back as
        one ZIP containing one image per page, named <name>_p01.png,
        <name>_p02.png ... in the order requested. Use this when the user wants
        a page as a picture (to share, embed in a slide, or post), a thumbnail,
        or a page for another image tool. To read a PDF's text, use
        extract_text instead.
        """, reply=Reply.FILES, returns=list, inputs=upload())

    add_operation_tool(mcp, "images_to_pdf", """Combine images into one PDF, one image per page.

        Pass several images in `files` (one page each, in the order given), or
        one file in file_content / file_id: an image (PNG, JPEG, WebP, TIFF, GIF,
        BMP; every page of a multi-page TIFF becomes a page, GIF / WebP
        animations give their first frame) or a ZIP of images, whose pages follow
        the file names in natural order (page2 before page10; files that are not
        images are skipped and listed). When the user's images are upload
        references (e.g. [Uploaded Artifact: "a.jpg"]), call store_file once per
        upload with the reference in its file_content, then pass the file_ids in
        files. Photos are turned upright from their EXIF orientation,
        transparency becomes white, and JPEGs are embedded unchanged (no quality
        loss).
        """, reply=Reply.FILES, returns=list, inputs=IMAGE_FILES)
