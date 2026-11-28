# Morphyard

[![License: MIT](https://img.shields.io/github/license/xianhong1208/morphyard)](LICENSE)
[![CI](https://github.com/xianhong1208/morphyard/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/xianhong1208/morphyard/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/xianhong1208/morphyard)](https://github.com/xianhong1208/morphyard/releases/latest)

**A document-conversion MCP server: hand it a file, get it back in the format you need — Office ↔ PDF, Markdown ↔ Word, spreadsheets, presentations, images, OCR and PDF page tools — from any MCP client.**

Formerly AnyDoc; see [renamed variables](docs/configuration.md#renamed-variables) if you are upgrading.

[繁體中文](README.zh-TW.md) · [Tools and formats](docs/tools.md) · [Configuration](docs/configuration.md) · [Architecture](docs/architecture.md) · [Roadmap](ROADMAP.md) · [Changelog](CHANGELOG.md)

Morphyard exposes its conversions as MCP tools that any MCP-capable client (Claude Code, Claude Desktop, Cursor, FastMCP clients) can call. Authentication is delegated to [MCP Gate](https://github.com/xianhong1208/MCP_Gate): Morphyard is an OAuth 2.1 resource server that verifies MCP Gate's RS256 tokens offline through its JWKS. It never stores users or issues tokens itself.

## What a call looks like

A Word file goes in as base64; a PDF comes back with a summary the model can relay, a download link and a `file_id` (the file is kept on the server for an hour), and — when it is at most 10 MB — the file itself as an MCP embedded resource, as before. From a real run against a local server (FastMCP client):

```python
await client.call_tool("convert_document", {
    "file_content": "<base64 of report.docx>",
    "file_name": "report.docx",
    "target_format": "pdf",
    "output_name": "report",
})
```

```text
TEXT  Conversion complete: report.pdf
      Path: docx → pdf (engines: libreoffice)
      Size: 23.4 KB

      Download link (valid until 2026-11-24 09:56 UTC, about 60 min):
      - report.pdf (23.4 KB, file_id f_o2UZNq6ujcit_LQkGJ6Rcg): https://morphyard.example/files/f_o2UZNq6ujcit_LQkGJ6Rcg/report.pdf?exp=…&sig=…
      To use a result in another Morphyard tool, pass its file_id instead of file_content.
FILE  file:///report.pdf  application/pdf  23963 bytes
LINK  https://morphyard.example/files/f_o2UZNq6ujcit_LQkGJ6Rcg/report.pdf?exp=…&sig=…  report.pdf  23963 bytes
```

The next tool can work on that result by `file_id` instead of the bytes (`{"text": "DRAFT", "file_id": "f_o2UZ…"}` to `pdf_watermark`). A result over 10 MB comes back as the link only, so it never floods the model's context; `MORPHYARD_RESULT_DELIVERY` switches between `auto`, `inline` (exactly 1.3), `both` and `link` — see [Delivery modes](docs/configuration.md#delivery-modes).

And `extract_text` on the same file returns Markdown for the model to read:

```markdown
# Quarterly report

| Region | Sales |
|--------|-------|
| North  | 120   |
| South  | 95    |

- North leads this quarter
```

## What's new

**1.2.1** (2026-11-09): LibreOffice conversions can no longer read local files or fetch URLs a document links to, and never run macros — the same guarantee Pandoc conversions got in 1.2.0, which also added CI that runs every advertised conversion inside the Docker image. See the [CHANGELOG](CHANGELOG.md), and the [Roadmap](ROADMAP.md) for what comes next.

## Key features

- **One contract for every tool.** `convert_document`, `extract_text`, `inspect_document`, `list_supported_conversions`, `pdf_extract_pages`, `pdf_split`, `pdf_rotate`, `pdf_protect`, `pdf_unlock`, `pdf_compress`, `pdf_metadata`, `pdf_strip_metadata`, and the page tools `pdf_merge`, `pdf_delete_pages`, `pdf_reorder_pages`, `pdf_watermark`, `pdf_page_numbers`, `pdf_to_images`, `images_to_pdf`, plus `job_status`, `job_cancel` and `job_list` for long conversions, `store_file` / `create_upload` to put files on the server first, and `pipeline` to run several of them in a row. Every single-file tool takes the same `file_content` / `file_name` / `mime_type` triple, so a host that injects uploaded files as base64 works with all of them, or a `file_id` of an earlier result, or a `url` to download the file from; `pdf_merge` and `images_to_pdf` take `files`, a list of `{file_content, file_name}`, `{file_id}` or `{url}` items.
- **PDF page tools.** Merge PDFs in order with one bookmark per file; delete pages; put pages in a new order (every page listed once, so none is lost by accident); add a text or image watermark (centre, diagonal or tiled, with opacity, colour and rotation); add page numbers (`{page} / {total}`), a header and a footer. Watermarks and page numbers sit upright as the reader sees each page, on mixed page sizes and rotated pages, and Chinese text is drawn with an embedded font.
- **PDF compression, passwords and metadata.** `pdf_compress` shrinks a PDF (lossless restructuring, or images downsampled and re-encoded at `balanced` / `small`) and reports the sizes; `pdf_protect` sets an AES-256 open password and/or permissions (print, copy, modify, annotate, fill forms) under an owner password; `pdf_unlock` removes them given the password; `pdf_metadata` reports Info, XMP, encryption, scripts and attachments; `pdf_strip_metadata` removes them.
- **Six engines, chosen by fidelity.** LibreOffice for layout-faithful Office ↔ PDF, Pandoc for structure-preserving text formats, [firecrawl-anydoc](https://pypi.org/project/firecrawl-anydoc/) for fast document → Markdown extraction, Tesseract for OCR, Pillow for images, and a thin text engine for JSON / XML / TSV. A missing engine disables its paths at startup instead of failing at request time.
- **Multi-hop planning that says what it costs.** When no engine converts A → B directly, the registry searches up to three hops, ranked by fidelity. Lossy hops are allowed by default but flagged in the result so the model can tell the user; pass `allow_quality_loss=false` to refuse any path that would lose layout.
- **Format detection without a file name.** Extension → MIME → container inspection (ZIP directory / OLE2 streams) → magic bytes. Sixteen formats are recognised with no name and no MIME type at all.
- **Documents cannot reach out.** Conversions never read local files or fetch URLs a document points at, and LibreOffice never runs macros.
- **Standards-based auth.** Bearer tokens are verified against MCP Gate's JWKS (issuer, audience, optional scopes); the server publishes `/.well-known/oauth-protected-resource/mcp` so OAuth-aware clients discover where to sign in. With the optional revocation feed, a token revoked in MCP Gate stops working here within one poll interval (30 s by default). With the optional tool policy (`MORPHYARD_TOOL_POLICY_ENABLED=true`), only tools an admin approved in MCP Gate are listed and callable, MCP Gate's access rules apply to each token, and a call that lacks a scope gets an HTTP 403 step-up challenge; see [Tool approvals and access rules](docs/configuration.md#tool-approvals-and-access-rules).
- **Results as download links.** Every result file is kept on the server for an hour (settable, at most a day) and the reply carries an expiring signed link and a `file_id`, which any tool takes as input, so a multi-step job (convert → watermark → compress) never sends a file back through the model. Results up to 10 MB still come back inline too (`auto`), so hosts that turn inline files into artifacts keep working; larger ones are a link only. Caps per person and in total refuse new files rather than delete unexpired ones. See [docs/configuration.md](docs/configuration.md#stored-files-and-download-links).
- **Long conversions as jobs.** A call answers within 20 s: a conversion that takes longer (a 300-page document to PDF) answers with a `job_id` and keeps running, and `job_status` returns the result, `job_cancel` stops it (its LibreOffice processes included) and `job_list` lists your jobs. Only the person who started a job can see it; caps per person and in total; progress notifications when the client asks for them. Jobs live in memory, so a restart loses them. See [Jobs](docs/configuration.md#jobs).
- **Files by URL, safely.** Give a tool `url` instead of the file and Morphyard downloads it, fetching public addresses only: every address the host resolves to is checked, the connection goes to the checked address (no DNS rebinding), redirects are checked hop by hop, and size and time are capped. `MORPHYARD_URL_ALLOW` opens chosen LAN hosts. See [URL inputs](docs/configuration.md#url-inputs).
- **Pipelines.** `pipeline` runs up to 10 tools in a row on the server as one call: merge → compress → watermark gives one PDF, split → rotate rotates every part, and one tool over 20 files works as a batch. Intermediate files stay on the server; only the final file(s) come back, as links or one ZIP. Every step is checked before anything runs, and an error names the step. The whole pipeline is one job, with progress counted in steps. See [Pipelines](docs/tools.md#tools).
- **Several files per call.** `pdf_merge` and `images_to_pdf` take up to 20 files (200 MB) in one call. A host that injects an upload into one `file_content` per call stores each upload with `store_file`, which answers with a `file_id`, and the model then passes all the `file_id`s at once. Clients and scripts that can send HTTP get a signed, single-use upload URL from `create_upload` and send the bytes with `curl -T`, so no base64 goes through the conversation. See [Several files per call](docs/configuration.md#several-files-per-call-and-uploads).
- **Runs anywhere.** One Docker image with all engines baked in, or a plain `uv run`.

Supported: pdf, docx, doc, odt, rtf, epub; xlsx, xls, ods, csv, tsv; pptx, ppt, odp; md, html, txt, rst, tex, json, xml; png, jpg, webp, gif, bmp, tiff. The full tool and format tables are in [docs/tools.md](docs/tools.md).

## Installation

**Prerequisites:** Python 3.13+ and [uv](https://docs.astral.sh/uv/). The Python engines (`firecrawl-anydoc`, `pypdf`, `pikepdf`, `pillow`) install with `uv sync`; the others are system packages:

```bash
# Debian / Ubuntu
sudo apt-get install -y libreoffice-writer libreoffice-calc libreoffice-impress \
                        pandoc tesseract-ocr tesseract-ocr-chi-tra tesseract-ocr-chi-sim \
                        tesseract-ocr-eng poppler-utils fonts-noto-cjk fonts-dejavu-core
```

`fonts-noto-cjk` is not optional: without it LibreOffice renders CJK text in PDFs as `□□□` and reports success. Tesseract language packs are not optional either — a missing pack makes Tesseract fail to start rather than degrade.

```bash
git clone https://github.com/xianhong1208/morphyard.git
cd morphyard
uv sync
```

## Quick start

### 1. Start MCP Gate and register Morphyard

Run [MCP Gate](https://github.com/xianhong1208/MCP_Gate) (default `http://localhost:4568`) and register a service pointing at this server: host `127.0.0.1`, port `5055`, path `/mcp`. MCP Gate derives the token audience from that registration (`http://127.0.0.1:5055/mcp`); Morphyard derives the same value from `MORPHYARD_BASE_URL`, so the two only need to agree on host and port.

### 2. Start Morphyard

```bash
cp .env.example .env          # defaults already point at http://localhost:4568
uv run python main.py
```

The startup log lists which engines are available. Open `http://localhost:5055/` for the landing page and `http://localhost:5055/docs` for the REST API.

### 3. Connect a client

Copy the ready-made snippets from the service page in MCP Gate, or by hand:

```bash
# Claude Code (OAuth: completes sign-in through MCP Gate on first use)
claude mcp add --transport http morphyard http://localhost:5055/mcp

# Any client with a personal access token issued by MCP Gate
claude mcp add --transport http morphyard http://localhost:5055/mcp \
  --header "Authorization: Bearer <token>"
```

Then ask your assistant to convert a file, e.g. "turn this DOCX into a PDF".

### 4. Verify

```bash
curl -s http://localhost:5055/.well-known/oauth-protected-resource/mcp   # points at MCP Gate
curl -s http://localhost:5055/.well-known/oauth-authorization-server      # MCP Gate's metadata, re-served for older clients
curl -i -X POST http://localhost:5055/mcp                                  # 401 without a token
```

For a local experiment without MCP Gate, start it with authentication off and run the protocol smoke test:

```bash
AUTH_ENABLED=false SERVER_PORT=5056 MORPHYARD_BASE_URL=http://127.0.0.1:5056 MORPHYARD_REVOCATION_ENABLED=false uv run python main.py
uv run python scripts/mcp_smoke.py
```

## Deployment options

- **Docker** — skip the system packages; the image ships every engine and font:

  ```bash
  docker build -t morphyard .
  docker run -p 5055:5055 -v morphyard-data:/app/data -e MCP_GATE_URL=http://mcp-gate:4568 \
    -e MORPHYARD_BASE_URL=http://127.0.0.1:5055 morphyard
  ```

  The `/app/data` volume keeps stored files and the download-link signing key across container re-creates.

- **Directly on the host** with `uv run python main.py`, as in the quick start.

Either way, `MORPHYARD_BASE_URL` must be the address clients use, because the token audience is derived from it. Every setting, and how to turn on the revocation feed, is in [docs/configuration.md](docs/configuration.md).

## Documentation

| Document | What is in it |
|---|---|
| [Tools and formats](docs/tools.md) | The tools, supported formats, how a conversion runs, known limits |
| [Configuration](docs/configuration.md) | Environment variables, concurrent conversions, the MCP Gate revocation feed, tool approvals and access rules |
| [Architecture](docs/architecture.md) | Module layout, engines and fidelity, format detection, conversion planning, test layers |
| [Development](docs/development.md) | Setting up, the three test layers, CI, how to contribute |
| [Roadmap](ROADMAP.md) | What shipped and what comes next |
| [Changelog](CHANGELOG.md) | Every release |

## Contributing

Bug reports and pull requests are welcome. Pull requests go into `dev`; run `uv run ruff check .` and `uv run pytest` first. See [docs/development.md](docs/development.md).

## Licence

[MIT](LICENSE)

### Third-party licences

Morphyard's own code is MIT, and it uses no AGPL library (no PyMuPDF, no Ghostscript). It runs the
conversion engines as separate programs; the Docker image ships them unmodified, each under its own
licence: LibreOffice (MPL-2.0), Pandoc (GPL-2.0-or-later), Poppler's utilities (GPL), Tesseract
(Apache-2.0) and the Noto CJK fonts (SIL OFL 1.1). These licences cover the tools, not the files
you convert with them. The PDF page tools draw text with reportlab (BSD) and embed subsets of
DejaVu Sans (Bitstream Vera licence) and Droid Sans Fallback (Apache-2.0), both of which allow
embedding.
you convert with them. The Python library pikepdf (MPL-2.0), used for PDF compression,
encryption and metadata, bundles qpdf (Apache-2.0).
