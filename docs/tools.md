# Tools and formats

[繁體中文](tools.zh-TW.md) · [README](../README.md)

## Tools

| Tool | Purpose | Returns |
|---|---|---|
| `convert_document` | Convert to a target format | File |
| `extract_text` | Extract content as Markdown for the model to read | Text |
| `inspect_document` | Format, size, page count and reachable targets | Text |
| `list_supported_conversions` | Capability matrix (only engines that are installed) | Text |
| `pdf_extract_pages` | Pick or reorder pages | File |
| `pdf_split` | Split into several files | Files |
| `pdf_rotate` | Rotate selected pages | File |
| `pdf_protect` | AES-256: open password and/or permissions under an owner password | File |
| `pdf_unlock` | Remove the password and permissions, given the password | File |
| `pdf_compress` | Make a PDF smaller (`lossless` / `balanced` / `small`) | File |
| `pdf_metadata` | Info, XMP, version, pages, encryption, scripts, attachments | Text |
| `pdf_strip_metadata` | Remove metadata (and optionally scripts / attachments) | File |
| `pdf_merge` | Merge 2–20 PDFs (`files`) in order, one bookmark per file | File |
| `pdf_delete_pages` | Remove selected pages | File |
| `pdf_reorder_pages` | Put every page in a new order (`3,1,2,4-10`, `10-1`, `reverse`) | File |
| `pdf_watermark` | Text or image (PNG/JPEG) watermark: centre, diagonal or tiled; opacity, colour, rotation; all or chosen pages | File |
| `pdf_page_numbers` | Page numbers (`{page}`, `{total}`), header and footer text; position, start number, skip the first page, font size, margin | File |
| `pdf_to_images` | Render pages as PNG, JPEG or WebP, one image per page | File (one page) or ZIP |
| `images_to_pdf` | Several images (`files`), one image, a multi-page TIFF or a ZIP of images → one PDF, a page per image | File |
| `job_status` | The result of a conversion that was still running when its tool answered (waits up to 20 s) | What the tool returns |
| `job_cancel` | Stop a queued or running conversion | Text |
| `job_list` | Your conversions: queued, running and recently finished | Text |
| `store_file` | Keep one (injected) file on the server as it is and get its `file_id` | Text |
| `create_upload` | A signed, single-use URL to `PUT` one file to, and the `file_id` it will have | Text |

**Long conversions.** Every tool that converts answers within 20 s: if the work is not done by then, the reply gives a `job_id` and the conversion goes on; `job_status` returns the result when it is ready. See [Jobs](configuration.md#jobs).

**PDF pages to images.** `pdf_to_images` renders with Poppler's `pdftoppm`: `image_format` `png` (default), `jpg` or `webp`; `dpi` 36–300 (default 150); `pages` like `1-3,7` (empty = all, at most 50 per call); `quality` 1–95 for JPEG / WebP. A page that would exceed 25 megapixels at the requested DPI is rendered at a lower DPI, and the reply says so. One page comes back as the image; several come back as one ZIP (`<name>_p01.png`, …), because some clients turn image resources into model input and a ZIP is one download. The images together may not exceed 50 MB. Password-protected PDFs are refused (a PDF with only an owner password renders). `convert_document` from PDF to an image format points the model at this tool.

**Images to a PDF.** `images_to_pdf` takes several images in `files` (one page each, in the order given; each item a `file_id` or base64, a PDF or ZIP among them is refused), or one file: an image (PNG, JPEG, WebP, TIFF, GIF, BMP), every page of a multi-page TIFF, the first frame of a GIF / WebP animation, or a ZIP of images whose pages follow the file names in natural order (`page2` before `page10`; non-images, `__MACOSX/` and hidden files are skipped and listed). `page_size` `fit` (default, each page the size of its image at the resolution the file records, else 96 DPI), `a4` or `letter` (image scaled to fit inside the margins and centred); `orientation` `auto` (landscape for wide images), `portrait` or `landscape`; `margin_mm` 0–50 (default 0 for `fit`, 10 otherwise). EXIF orientation is applied; JPEGs (and the main image of a camera MPO file) are embedded byte for byte, with the orientation applied by the placement on the page; other images are stored losslessly; transparency becomes white; a matching ICC profile is kept. Limits: 100 pages, 50 megapixels per image, 400 megapixels in all, 500 ZIP entries, 200 MB unpacked, 100 MB of output. HEIC / AVIF are not read yet (1.6). `convert_document` from one image to `pdf` uses the same code with `fit` pages; before, it OCRed the picture and returned its text in a PDF.

### PDF page tools

- **Input.** `pdf_merge` takes `files`, a list of `{file_id}` items (a file kept on the server) or `{file_content, file_name}` items (base64). A host's upload injection fills only one `file_content` per call, so uploads are first stored one by one with `store_file` and then passed by `file_id` (see [Several files per call](configuration.md#several-files-per-call-and-uploads)). The other page tools take the usual triple; `pdf_watermark`'s image goes in `watermark_image` as base64.
- **Pages.** Page selections use the same 1-based syntax as `pdf_extract_pages` (`1-3,7,10-`). `pdf_reorder_pages` insists that every page appears exactly once and names the missing or repeated ones; use `pdf_delete_pages` to drop pages.
- **Kept.** Merge nests each file's bookmarks under a bookmark named after the file. Delete and reorder keep the bookmarks whose page survives, pointing at its new place. Watermark and page numbers keep bookmarks, links, form fields and metadata.
- **Placement.** Watermarks, numbers, headers and footers are drawn in the page as the reader sees it (the crop box, turned by the page's rotation), so they sit upright on landscape and rotated pages and on documents that mix page sizes. A single watermark or a header too long for the page is shrunk to fit.
- **Numbering.** Only stamped pages are counted: `{page}` runs `start_number`, `start_number + 1`, … over them and `{total}` is the last number shown, so skipping a cover gives "1 / 9" … "9 / 9" on a 10-page file.
- **Fonts.** Text is drawn with DejaVu Sans (Latin) and Droid Sans Fallback (Chinese, Japanese, Korean), embedded as subsets, switching per run of characters. Noto CJK cannot be used here: it is OpenType/CFF, which reportlab cannot embed. Without Droid Sans Fallback installed, CJK text uses a non-embedded CID font and viewers substitute their own.
- **Limits.** Merge: 2–20 files, 50 MB together. Every page tool: 2,000 pages per document, 100 MB output, 200 characters of text, watermark image 10 MB / 40 megapixels. Encrypted PDFs are refused; remove the password first.

**Supported formats**

| Category | Formats |
|---|---|
| Documents | pdf, docx, doc, odt, rtf, epub |
| Spreadsheets | xlsx, xls, ods, csv, tsv |
| Presentations | pptx, ppt, odp |
| Text | md, html, txt, rst, tex, json, xml |
| Images | png, jpg, webp, gif, bmp, tiff |

Not supported: audio, video, archives, CAD, executables. The tool descriptions and [`config/instructions.md`](../config/instructions.md) tell the model to say so instead of retrying.

## PDF compression, passwords and metadata

- **`pdf_compress`** — `lossless` restructures the file only (object streams, recompressed streams, unused objects removed): pages are unchanged. `balanced` (default) also downsamples images above 150 DPI and re-encodes them as JPEG quality 75; `small` uses 96 DPI and quality 50. `max_dpi` (36–600) and `jpeg_quality` (10–95) override the level. The DPI is measured from where each image is painted, so an image shown small is downsampled more. Text, fonts and vector graphics are never degraded. If the result is not at least 1 % smaller, the original is returned and the answer says so. Scans shrink most (a six-page 300 DPI scan: 11.0 MB → 1.8 MB balanced, 0.64 MB small); text-only PDFs shrink by restructuring alone (28–51 % on the PDFs measured).
- **`pdf_protect`** — AES-256. `password` is the open password; `owner_password` guards the permissions `allow_print`, `allow_copy`, `allow_modify`, `allow_annotate`, `allow_fill_forms` (all allowed by default). Any restriction needs an owner password that differs from the open password. In the PDF format the annotate permission includes filling forms, so blocking form filling also needs `allow_annotate=false`. Permissions are honoured by standard readers; they are not DRM.
- **`pdf_unlock`** — returns an unencrypted copy given the password. The owner password always works; the open password only when nothing is restricted. No password recovery.
- **`pdf_metadata`** — read only: version, pages, encryption, Info, XMP, annotation authors, scripts and actions, embedded files, forms.
- **`pdf_strip_metadata`** — always removes the Info dictionary, every XMP stream and PieceInfo, and gives the file a new identifier; by default also annotation author names and dates (not form field names). Optionally scripts and actions (`remove_javascript`) and embedded files (`remove_attachments`). Names written in the page text are not touched.

Every tool except `pdf_unlock` (and `pdf_metadata`, which reports it) refuses an encrypted PDF and points to `pdf_unlock`. Limits: 5,000 pages; images over 100 megapixels are not re-encoded.

## How it works

```
client ──(bearer token)──▶ /mcp ──▶ tools ──▶ service.py ──▶ registry.plan_conversion() ──▶ engines
                             │
                             └── JWTVerifier(jwks_uri = MCP_GATE_URL/.well-known/jwks.json)
```

1. **Input.** Tools receive `file_content` (base64), `file_name` and `mime_type`, or instead a `file_id`: a result an earlier Morphyard tool returned, still on the server (see [Chaining tools with file_id](configuration.md#chaining-tools-with-file_id)). Hosts that manage uploads usually inject the base64 automatically; the parameter descriptions are written so the model puts the upload reference in `file_content` and nothing else.
2. **Detection.** `detect_format()` tries the extension, then MIME, then looks inside the container, then magic bytes — because in practice the file name is often missing.
3. **Planning.** `plan_conversion()` finds a direct engine or a path of up to three hops, ranked by fidelity, hop count and intermediate-format preference. Direct paths are never refused. Multi-hop paths that drop below `structural` fidelity are executed with a warning attached; with `allow_quality_loss=false` they are refused instead.
4. **Execution.** Engines run as subprocesses inside a temporary workspace with a timeout; scanned PDFs fall back from text extraction to OCR automatically.
5. **Output.** A result file is kept on the server for an hour and the reply carries its download link and `file_id` in the text and as an MCP `resource_link`; by default (`auto`) a file up to 10 MB also arrives inline as an MCP `EmbeddedResource`, as before 1.4. See [Delivery modes](configuration.md#delivery-modes).

| Term | Meaning |
|---|---|
| **Fidelity** | `high` (layout preserved), `structural` (headings / lists / tables preserved), `lossy` (text only). Drives path planning. |
| **Hop** | One engine invocation in a multi-step path. |
| **Audience** | The resource URI in the token; must equal what MCP Gate registered for this server. |

The engine table, the planner's rules and the format-detection details are in [docs/architecture.md](architecture.md).

## Known limits

- One file per call, except `pdf_merge` and `images_to_pdf`, which take up to 20 files (200 MB) in `files`. A host that can inject only one upload per call stores each with `store_file` and passes the `file_id`s. Other tools take several files one call at a time. A multi-step job (convert, then watermark, then compress) passes each result on by `file_id`, so no file travels back through the model.
- 50 MB per file, 30 pages per OCR run.
- A limited number of conversions run at once (by default the CPU count, at most 8); more wait in a queue, and past its length or wait time a tool answers "the server is busy" and should be called again a little later. See [Concurrent conversions](configuration.md#concurrent-conversions).
- PDF → editable formats is always lossy; tables inside PDFs are recovered heuristically.
- Images are kept but appended at the end of extracted Markdown, not at their original position.
- `.xlsb` is not supported.
- Pandoc conversions never read files or URLs a document points at: LaTeX `\input` / `\lstinputlisting` / `\includegraphics` and images given as a path or `http(s)` URL are dropped (an image keeps its alt text); images embedded in the document or given as `data:` URIs are kept.
- LibreOffice conversions never read files or URLs a document links to either: an image linked by path or URL (an HTML `<img>`, a DOCX/XLSX/PPTX relationship marked external, an ODF `xlink:href`, an RTF `INCLUDEPICTURE` field) is left out of the output, and stylesheets are not fetched. Every conversion runs with a LibreOffice profile that blocks links from untrusted documents and sends all HTTP/HTTPS/FTP traffic to a proxy that cannot exist, and ODF, OOXML and RTF documents are stripped of outside references first (by content, so an ODP renamed to `.pptx` is treated as ODP). Images stored inside the document are kept. A zip-based document that cannot be read far enough to check it is refused instead of converted.
- Revocation is not instant: with the [revocation feed](configuration.md#token-revocation) a revoked token stops working within one poll interval (30 s by default); without it, at expiry.
