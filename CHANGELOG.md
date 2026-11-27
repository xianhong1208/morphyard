# Changelog

All notable changes to Morphyard (formerly AnyDoc) are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Stored files and signed download links (#33, first part of 1.4). Files can be kept on the server in `MORPHYARD_DATA_DIR` (default `./data`; the Docker image declares a volume at `/app/data`) for `MORPHYARD_FILE_TTL_SEC` (1 h by default, 24 h at most) and downloaded from `GET /files/<id>/<name>?exp=…&sig=…`, an HMAC-signed link that anyone holding it can use until it expires. A wrong, expired or tampered link and a removed file all get the same 404; downloads are attachments with `nosniff`, `no-store` and a sandbox CSP; signatures never reach the logs. Caps of 2 GB in total and 512 MB per person (`MORPHYARD_STORE_MAX_MB`, `MORPHYARD_STORE_OWNER_MAX_MB`) refuse new files with `storage_full` instead of deleting unexpired ones; expired files are swept every minute and at startup, and unexpired ones survive a restart. The signing key comes from `MORPHYARD_SIGNING_KEY` or is generated once into `<data_dir>/.signing_key` (mode 600); if it is lost or changes while files are stored, those files are deleted at startup with a warning, since their links can no longer be checked. `MORPHYARD_PUBLIC_URL` (default `MORPHYARD_BASE_URL`) sets the address links point at. `GET /health` gains a `store` block (counts and caps, no names). Tools return those links since the next entry.
- Results as download links, and `file_id` inputs (#33, second part of 1.4). Every file a tool produces is kept in the store for its caller (the token's `sub`; `anonymous` with authentication off), and the reply's text lists each file's name, size, `file_id`, download URL and expiry, followed by an MCP `resource_link` per file (the URL is in the text too, because some clients, Claude Code among them, ignore `resource_link`). `MORPHYARD_RESULT_DELIVERY` picks what else comes back: `auto` (default) also returns each file up to `MORPHYARD_INLINE_MAX_MB` (10 MB) inline exactly as in 1.3 and larger ones as a link only, with text telling the model the file is at the URL; `inline` is the 1.3 reply with nothing stored; `both` always inlines; `link` never does. Every tool that takes a file accepts `file_id` instead of `file_content` (and `pdf_merge` items `{file_id}`), so convert → watermark → compress runs without a file passing back through the model; a `file_id` that is unknown, expired or someone else's gets one and the same refusal, and giving both `file_content` and `file_id` is refused. A result that does not fit the store is a tool error naming the limit and when to retry, and nothing of it is kept (a split is stored whole or not at all). The model instructions now say to give the user the link and to pass results on by `file_id`. `scripts/mcp_smoke.py` takes `MORPHYARD_SMOKE_URL` and checks a link and a `file_id` chain.
- Long conversions as jobs (#34, 1.4). Every tool that converts (all but `list_supported_conversions`, `inspect_document` and `pdf_metadata`) runs as a job: the call waits up to `MORPHYARD_JOB_WAIT_SEC` (20 s, at most 120) and, if the conversion is done, answers exactly as before. If not, it answers with one text block giving a `job_id` and how to poll, and the conversion goes on. New tools: `job_status(job_id, wait_sec)` waits (at most `MORPHYARD_JOB_WAIT_SEC`) and returns what the original tool would have returned (files with links under the delivery mode, text, or the same error); `job_cancel(job_id)` stops a job, killing its engine's process group (LibreOffice and the processes it starts), so its slot goes to the next job in the queue; `job_list()` lists your jobs. A job is seen only by its owner (the token's `sub`): another person's id gets the same "unknown" answer as an id that never existed. Caps: `MORPHYARD_MAX_JOBS_PER_OWNER` (5) queued or running per person and `MORPHYARD_MAX_JOBS` (64) in all, refused at once; `MORPHYARD_JOB_MAX_SEC` (1800) stops a job, queue wait included. Jobs wait for a conversion slot in FIFO order without the queue timeout (the queue's length limit still refuses at once). Progress goes out as MCP `notifications/progress` when a request carries a `_meta.progressToken` (queue position, start, engine steps), on the original call and on `job_status`. A finished job and its result are kept as long as files (`MORPHYARD_FILE_TTL_SEC`). Jobs live in memory: a restart loses them. `GET /health` gains a `jobs` block. Tool descriptions and the model instructions tell the model to poll `job_status` rather than call the tool again.
- Files by URL (#35, 1.4). Every tool that takes a file accepts `url`, an http or https address to download it from (and `pdf_merge` items `{url}`), next to `file_content` and `file_id`; giving more than one of the three is refused. A guard against server-side request forgery: only http/https, no `user:pass@`; every address the host resolves to must be public (loopback, private, link-local and cloud metadata, carrier-grade NAT, benchmark, reserved, multicast and `0.0.0.0` are refused, also inside IPv4-mapped, 6to4 and NAT64 IPv6 addresses and in spellings such as `2130706433` or `0x7f.1`); the connection goes to the address that was checked, so DNS rebinding cannot switch it; redirects are followed by hand, at most 5, each hop checked again, never https → http; the body is capped at `MAX_INPUT_MB` while it streams, after gzip/deflate decoding (a lying `Content-Length` or a decompression bomb stops at the cap); 10 s to connect, 15 s idle, `MORPHYARD_URL_TIMEOUT_SEC` (60 s) in all; no proxy, cookies or credentials. `MORPHYARD_URL_ALLOW` opens LAN networks or host names, `MORPHYARD_URL_DENY` closes networks or hosts (deny wins), `MORPHYARD_URL_FETCH=false` turns URL input off. A download link of this server is read from the store after its signature check instead of fetched. Logs show the scheme, host and port only, never the path or query string (the DEBUG log of tool arguments cuts every `url` to `scheme://host:port/…`). No new dependency (httpcore and certifi were already installed with httpx).

### Changed
- Morphyard now keeps state on disk (`MORPHYARD_DATA_DIR`). It is disposable: deleting it loses at most an hour of stored files and their links.
- The tool list gains `job_status`, `job_cancel` and `job_list` (22 tools), and the 16 tools that run as jobs gain one paragraph in their description (#34).
- MCP tool calls no longer give up after `MORPHYARD_QUEUE_TIMEOUT` in the conversion queue: they wait as jobs (bounded by `MORPHYARD_JOB_MAX_SEC`). The setting still applies to other callers of the conversion service (#34).
- The model instructions no longer say only one file can be processed per call: `pdf_merge` takes several, other tools one per call (#34).
- File-producing tools' replies: the text gains the download lines, and `resource_link` blocks follow the files (see Added). The tool list gains an optional `file_id` parameter on the 17 tools that take a file, and `pdf_merge` items' `file_content` became optional next to `file_id`; names and order of the existing parameters are unchanged. The 17 tools also gain an optional `url` parameter after `file_id`, `pdf_merge` items a `url` field, and the "give one input" messages name all three (#35).

### Fixed
- A file larger than the per-person or total store cap was refused with "try again when older files expire" and a retry time, although it could never fit. It is now refused as never fitting (`never_fits`, no retry time).

### Upgrade notes
- **A conversion over 20 s now answers with a `job_id`** instead of the result. Hosts whose model reads tool descriptions or the server instructions learn to call `job_status`; a host that cannot make a second call can set `MORPHYARD_JOB_WAIT_SEC` up to 120 (keep it below the client's own timeout). Calls done within the wait are unchanged.
- With authentication off, all callers share one owner and so the per-person cap (`MORPHYARD_MAX_JOBS_PER_OWNER`, 5 at once): raise it for a shared LAN server without auth.
- **`auto` keeps small results inline.** Hosts that turn inline files into artifacts keep working unchanged for results up to 10 MB; results above that now come back as a link only. To keep the exact 1.3 replies (every result inline, nothing stored), set `MORPHYARD_RESULT_DELIVERY=inline`; to raise the threshold, set `MORPHYARD_INLINE_MAX_MB`.
- Links point at `MORPHYARD_PUBLIC_URL` (default `MORPHYARD_BASE_URL`): set it to the address people's browsers and agents reach the server at, or the links in replies will not open for them.

### Internal
- The tool bodies moved into an operations registry (`src/domain/operations/`: arguments, arity, formats and the work of each of the 19 tools), and the MCP tools became thin wrappers over it; no behaviour change, and the tool list is pinned by a snapshot test (#37).

## [1.3.0] — 2026-11-22

### Added
- `pdf_merge` (#24): merge 2–20 PDFs (50 MB together) in the given order into one. Each file gets a top-level bookmark named after it, with its own bookmarks nested underneath (`bookmarks=false` to skip). The files come as a list of `{file_content, file_name}` items in base64, since a host's upload injection fills only one file per call.
- `pdf_delete_pages` and `pdf_reorder_pages` (#26). Delete takes a page selection (`2,5-7`) and refuses to delete every page. Reorder takes the complete new order (`3,1,2,4-10`; a descending range such as `10-1` runs backwards; `reverse`) and refuses an order that leaves out or repeats a page, naming which. Bookmarks follow their pages.
- `pdf_watermark` (#25): a text watermark (font size, opacity, rotation, colour as hex or name) or a PNG/JPEG image watermark (`watermark_image`, base64; transparency kept; `image_scale`), in the centre, along the page diagonal or tiled, on all pages or a page selection.
- `pdf_page_numbers` (#28): page numbers in a format such as `{page} / {total}` or `第 {page} 頁`, in one of six positions, with a start number, an option to skip the first page and a page selection, plus header and footer text (left, centre or right; `{page}`/`{total}` allowed), font size, margin and colour.
- Watermarks, numbers, headers and footers are placed upright as the reader sees each page, on mixed page sizes and rotated pages. Text in Chinese, Japanese and Korean is drawn with Droid Sans Fallback and Latin text with DejaVu Sans, embedded as subsets. New dependency: reportlab (BSD); the Docker image adds `fonts-droid-fallback` (Apache-2.0).
- Limits for the page tools: 2,000 pages per document, 100 MB output, 200 characters of text, watermark images up to 10 MB and 40 megapixels. Encrypted PDFs are refused with a message saying to remove the password first.
- `pdf_compress` (#27): makes a PDF smaller with pikepdf/qpdf, no Ghostscript. `lossless` only restructures (object streams, Flate recompression, unused objects dropped; pages look identical); `balanced` also downsamples images above 150 DPI and re-encodes them as JPEG quality 75; `small` goes to 96 DPI and quality 50 (`max_dpi` / `jpeg_quality` override). The DPI is measured from where each image is painted on the page, form XObjects included. 1-, 2- and 4-bit images, CMYK, colour-key masks and decode arrays are left alone, and a re-encoded image is kept only if it is at least 10 % smaller. The answer gives the sizes before and after; if the file is not at least 1 % smaller, the original comes back unchanged and the answer says so. Measured: an 11.0 MB six-page 300 DPI scan → 1.8 MB (`balanced`) / 0.64 MB (`small`); a 15-page LibreOffice PDF in Chinese 310 → 224 KB at every level (text identical).
- `pdf_unlock` (#29): removes a PDF's password and permission restrictions, given its password. The owner password always unlocks; the open password unlocks only a file whose owner set no restrictions. A wrong password gets one plain message, the same for every kind of file.
- `pdf_metadata` (#30): reports a PDF's version, page count, encryption (method, permissions, whether it needs a password), Info dictionary, XMP properties, annotation authors, JavaScript and actions, embedded files and forms, without changing it. A password-protected file reports only that it needs its password, which can be passed in.
- `pdf_strip_metadata` (#30): returns a copy without the Info dictionary, any XMP stream (document, pages, images) or application data (PieceInfo), with a new file identifier; annotation author names and dates are removed too (`remove_annotation_authors`, on by default; form field names are kept). `remove_javascript` and `remove_attachments` also drop document scripts and actions, and embedded files.
- New dependency: pikepdf (MPL-2.0, bundles qpdf).
- `pdf_to_images` (#31): render PDF pages as PNG, JPEG or WebP with Poppler's `pdftoppm`, one image per page. Options: `image_format`, `dpi` (36–300, default 150), `pages` (at most 50 per call), `quality`. A page that would exceed 25 megapixels is rendered at a lower DPI and the reply says which. One page comes back as the image, several as one ZIP (`<name>_p01.png`, …); the images may total 50 MB. Password-protected PDFs are refused.
- `images_to_pdf` (#32): one image, a multi-page TIFF or a ZIP of images (natural file-name order, non-images skipped and listed) → one PDF, a page per image. `page_size` `fit` / `a4` / `letter`, `orientation` `auto` / `portrait` / `landscape`, `margin_mm`. EXIF orientation is applied, JPEGs are embedded unchanged, other images losslessly, transparency becomes white. Limits: 100 pages, 50 megapixels per image, 400 in all, 500 ZIP entries, 200 MB unpacked, 100 MB of output.

### Changed
- `pdf_protect` (#29) now uses pikepdf and can set permissions: `allow_print`, `allow_copy`, `allow_modify`, `allow_annotate`, `allow_fill_forms` under an `owner_password` (required for any restriction, and different from the open password). `password` (the open password) is now optional, so a PDF can be readable by anyone but not printable or copyable; called as before with only `password`, it does what it did. Still AES-256. An already encrypted PDF is refused with a pointer to `pdf_unlock`.

### Fixed
- `convert_document` from an image to PDF returned the picture's OCR text in a PDF (png → txt → docx → pdf). It now puts the picture on a page of its own size (new engine `images-to-pdf`). From PDF to an image format, its error now names `pdf_to_images`.

### Documentation
- README's Licence section lists the licences of the conversion engines in the Docker image.

## [1.2.2] — 2026-11-13

### Changed
- Renamed from AnyDoc to Morphyard; environment variables are now `MORPHYARD_*` (the `ANYDOC_*` names are no longer read): `MORPHYARD_BASE_URL`, `MORPHYARD_REVOCATION_ENABLED`, `MORPHYARD_REVOCATION_CLIENT_ID`, `MORPHYARD_REVOCATION_CLIENT_SECRET`, `MORPHYARD_REVOCATION_POLL_INTERVAL`, `MORPHYARD_REVOCATION_FAIL_MODE`. The Python project is `morphyard`, MCP serverInfo reports `Morphyard`, and the config section and tool module are `morphyard` / `src.fastmcp_tools.morphyard_tools`. The document → Markdown engine is now reported as `firecrawl` (it was `anydoc`, after the firecrawl-anydoc library it wraps). Port 5055 and the token audience `<base_url>/mcp` are unchanged.
- `MCP_CENTER_URL`, MCP Gate's name before it was renamed, is no longer read as a fallback for `MCP_GATE_URL`; set `MCP_GATE_URL` instead.

### Added
- A limit on concurrent conversions, with a queue (#23). There was none: every request started its engine at once, so a burst of uploads meant that many soffice processes competing for the same CPU and memory. Now at most `MORPHYARD_MAX_CONCURRENT_CONVERSIONS` conversions run at once (default: the CPUs the process may use, at most 8), across all engines together; a request holds one slot for its whole run, every hop included. Up to `MORPHYARD_MAX_QUEUED_CONVERSIONS` more (default 4 × the limit; `0` = no queue) wait first-in, first-out for up to `MORPHYARD_QUEUE_TIMEOUT` seconds (default 30). Past that a tool answers with an MCP tool error saying the server is busy, that nothing was started and to retry the same call shortly (`server_busy`, HTTP 503 with `Retry-After` if raised behind a REST route). Requests are validated before they queue, so a bad request is still answered at once. `GET /health` reports `conversions`: running, queued and the limits. `list_supported_conversions` and `inspect_document` take no slot.

### Fixed
- LibreOffice conversions no longer leave their profile behind (#22). Every conversion ran with a profile `lo_profile_<pid>_<n>` in the temp directory that was never deleted, so they piled up (about 900 on one server). Each conversion now creates its profile with `mkdtemp` (private, mode 0700, a name never reused, so a restarted container no longer reuses an old profile) and deletes it when the conversion ends: success, failure, timeout or a cancelled request. Before deleting it, nothing may still run with it: every engine now runs in its own process group, and a timeout or a cancelled request kills the whole group (killing only `soffice` left `soffice.bin` running), and soffice's own temp files (`lu….tmp`, left when it is killed) go into the profile. At startup the server removes stale profiles from earlier runs: only directories owned by its own user, untouched for an hour, not locked by a running conversion and not used by any running process, so other workers or servers sharing the temp directory keep theirs. The hardened profile settings from 1.2.1 are unchanged.

### Docs
- Docs: roadmap and README rewritten. A new `ROADMAP.md` / `ROADMAP.zh-TW.md` lists what shipped and the plan to 1.8, with a GitHub milestone per version. Both READMEs follow a shorter layout (badges, an example tool call from a real run, quick start, deployment options); the tool and format tables, known limits, configuration (with the renamed variables) and revocation-feed setup, and testing moved to `docs/tools`, `docs/configuration` and `docs/development` (English and Traditional Chinese). The local-experiment command no longer points at a `config/config.test.yaml` that does not exist.

## [1.2.1] — 2026-11-09

### Security
- LibreOffice conversions read files and fetched URLs a document linked to: an HTML `<img src="/usr/share/…">` or a DOCX image linked to `file:///…` came back embedded in the PDF, and an `<img>` or stylesheet naming `http://…` made AnyDoc request it (anything the server can reach). Every LibreOffice conversion now runs with a profile that blocks links from untrusted documents (`BlockUntrustedRefererLinks`), sends HTTP, HTTPS and FTP to an unreachable proxy (`proxy.invalid`, also set as `*_proxy` in soffice's environment) and never runs macros or updates links. Calc and Impress still loaded ODF images linked by path, and the RTF importer still read `INCLUDEPICTURE` files, despite the block, so documents are also stripped of outside references before LibreOffice opens them: external relationships in OOXML packages, `xlink:href`s (and `xml:base`, `form:image-data`) that do not point into the package in ODF, and linking fields (`INCLUDEPICTURE`, `INCLUDETEXT`, `IMPORT`, `LINK`, `DDE`, …) in RTF, whose stored result is shown instead. This goes by content, not by the declared format, because LibreOffice detects the type itself. Hyperlinks and images stored in the document are kept. A zip-based document that cannot be read far enough to check it (broken zip, malformed XML, an XML entity declaration) is now refused with a conversion error rather than passed on. Images linked by path or URL are no longer embedded; the same call as Pandoc's in 1.2.0.

## [1.2.0] — 2026-11-09

### Added
- **Continuous integration** (`.github/workflows/ci.yml`): ruff and pytest on every push and pull request to `dev` and `main`, and a job that builds the Docker image, starts it, and runs `scripts/sweep_routes.py --strict` and `scripts/mcp_smoke.py` inside the container against the engines and fonts that ship.

### Fixed
- `scripts/sweep_routes.py` exited 0 when routes failed, so nothing scripted could notice; it now exits 1, and with `--strict` also when a route was skipped for a missing engine.
- The Docker image's `CMD` ran `uv run`, which syncs the default dependency groups: every new container downloaded pytest and ruff before starting, or failed to start without network. It now runs `uv run --no-sync`.

### Security
- Pandoc conversions could read any file the service can, and fetch URLs: a `.tex` upload with `\lstinputlisting{/path}` came back with that file's contents, and an image naming a local path or URL was pulled into a DOCX, ODT, PPTX, RTF or EPUB. A conversion now reads the document with `--sandbox` into pandoc's AST, then writes from an AST holding no file references: an image stays only when it is embedded in the source or a `data:` URI (otherwise it becomes its alt text), metadata is limited to title, author, date and similar fields (the EPUB writer opens `cover-image`, `css` and `stylesheet`), and raw HTML is dropped from EPUB output. The whole conversion cannot run with `--sandbox`: Debian's pandoc reads its DOCX/PPTX/EPUB/LaTeX templates from disk, which the sandbox blocks. A conversion now takes about 65 ms longer (two pandoc runs).

## [1.1.1] — 2026-11-09

### Fixed
- AnyDoc reported **1.0.0** in `/openapi.json`, the landing page and MCP serverInfo after the 1.1.0 release: the version came from `app.version` in `config/config.yaml`, which the release did not bump. The version is now read from `pyproject.toml` (`src/version.py`), the one place it is written; `app.version` in a config is ignored.

## [1.1.0] — 2026-11-05

### Added
- **MCP Gate revocation feed** (opt-in: `ANYDOC_REVOCATION_ENABLED`, off by default). AnyDoc polls MCP Gate's `POST /oauth/revoked` with a confidential client bound to this server and rejects a revoked token within one poll interval (30 s by default) instead of at its expiry. Fail-open by default; feed health is reported under `revocation` in `GET /health`. See *Token revocation* in the README.
- The issuer is read from `MCP_GATE_URL` (`MCP_CENTER_URL` still works as a fallback).

### Changed
- **Renamed MCP Center to MCP Gate** in docs, configuration and identifiers.
- **fastmcp 4.** The OAuth discovery documents advertise the issuer exactly as configured.
- Converted files are labelled with their registered MIME types.

### Fixed
- `ENGINE_TIMEOUT` now applies to every conversion engine, so a hung engine can no longer block a request indefinitely.

### Security
- **Dependency advisories fixed: pip-audit 96 → 0.** pyjwt 2.13.0 → 2.15.1, authlib 1.7.2 → 1.9.4, joserfc 1.7.4 → 1.8.0, pypdf 6.15.0 → 6.21.4, dnspython 2.8.0 → 2.9.2, opentelemetry-api 1.44.0 → 1.45.2, and pillow 12.3.0 → 13.0.0 (major version; the floor in `pyproject.toml` is now `>=13.0.0`).

## [1.0.0] — 2026-09-10

First release: a document conversion MCP server (Office ↔ PDF, Markdown ↔ Word, spreadsheets, presentations, images, OCR, PDF split / extract / rotate / encrypt) secured by MCP Gate OAuth 2.1.

[Unreleased]: https://github.com/xianhong1208/morphyard/compare/v1.2.2...dev
[1.2.2]: https://github.com/xianhong1208/morphyard/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/xianhong1208/morphyard/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/xianhong1208/morphyard/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/xianhong1208/morphyard/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/xianhong1208/morphyard/releases/tag/v1.1.0
[1.0.0]: https://github.com/xianhong1208/morphyard/releases/tag/v1.0.0
