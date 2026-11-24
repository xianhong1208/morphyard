"""MCP tools: PDF pages to images, images to a PDF

Thin wrappers over the operations in src/domain/operations/pdf_image_ops.py
(which explains why several pages come back as one ZIP, and why images_to_pdf
takes a ZIP for several images). The input triple and the reply shape are the
same as in morphyard_tools.py.
"""

from fastmcp import FastMCP

from src.fastmcp_tools.operation_tool import Reply, add_operation_tool, upload
from src.log import get_mcptools_logger

logger = get_mcptools_logger()


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

        The uploaded file is either one image (PNG, JPEG, WebP, TIFF, GIF, BMP;
        every page of a multi-page TIFF becomes a page, GIF / WebP animations
        give their first frame) or a ZIP of images. Only one file can be passed
        per call, so for several images ask the user to put them in a ZIP: the
        pages follow the file names in natural order (page2 before page10), and
        files that are not images are skipped and listed. Photos are turned
        upright from their EXIF orientation, transparency becomes white, and
        JPEGs are embedded unchanged (no quality loss).
        """, reply=Reply.FILES, returns=list, inputs=upload())
