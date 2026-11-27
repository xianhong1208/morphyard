# Morphyard — document format conversion service

Converts the user's uploaded files into the format they need, and provides PDF
splitting, page extraction, rotation and encryption.

## When to use this service

The user has uploaded a file and mentions converting, changing, saving,
exporting or downloading it as some format, or wants to read / summarize a
document they uploaded.

## Service boundary (read this first; it saves wasted calls)

Only these five categories are handled:

| Category | Formats |
|---|---|
| Documents | pdf, docx, doc, odt, rtf, epub |
| Spreadsheets | xlsx, xls, ods, csv, tsv |
| Presentations | pptx, ppt, odp |
| Text | md, html, txt, rst, tex, json, xml |
| Images | png, jpg, webp, gif, bmp, tiff |

**Never supported: audio (mp3 / wav / m4a), video (mp4 / mov), archives
(zip / rar), CAD, executables.** When the user asks for one of these,
**tell them directly that this service cannot do it** and do not call any tool;
no combination of parameters will make it succeed.

Treat a tool response of "unsupported" or "could not recognize the format" the
same way: relay it to the user and do not retry with different parameters.
Repeated calls only slow the response down; the outcome will not change.

## Choosing a tool

| User intent | Tool to use |
|---|---|
| "What is this PDF about", "summarize this document" | `extract_text` — returns readable text |
| "Convert to PDF", "make a Word file", "save as JPG" | `convert_document` — returns a downloadable file |
| "What is this file", "how many pages" | `inspect_document` |
| "Which formats are supported", "can this be converted" | `list_supported_conversions` |
| "Only pages 3 to 5", "reorder the pages" | `pdf_extract_pages` |
| "One file per page", "split it up" | `pdf_split` |
| "These pages are sideways", "rotate them" | `pdf_rotate` |
| "Add a password", "protect this file" | `pdf_protect` |
| Several operations in a row ("merge, compress, then stamp DRAFT"), or one operation on many files | `pipeline` |

**The most common mistake is confusing `extract_text` with `convert_document`.**
The rule is simple: the user wants **you to read** the document -> `extract_text`;
the user wants **to receive a file** -> `convert_document`.

## Key rules of behaviour

1. **Never ask the user for the source format**; the service detects it from
   the file automatically.
2. **Always relay the warning attached to a conversion result; that is the
   condition under which lossy conversion is allowed.** Paths such as PDF back
   to Word only preserve text and images; layout, fonts and table styling are
   rebuilt. The service **converts anyway** rather than blocking you, but when
   the response contains a ⚠️, proactively tell the user: "the content and
   images are there, but the layout needs re-adjusting, and the images are
   collected at the end of the document". Reporting only "conversion complete"
   leaves the user to discover the problem when they open the file.
3. **When a conversion fails, read the error message before deciding the next step**:
   - `NO_CONVERSION_PATH` — this path is not feasible. Use
     `list_supported_conversions` to find alternatives and propose them to the
     user (e.g. PDF cannot go straight to PowerPoint, but the text can be
     extracted and rebuilt).
   - `UNSUPPORTED_FORMAT` — usually the file name has no extension; ask the
     user for the complete file name.
   - `CONVERSION_FAILED` — the file itself may be corrupt or password-protected;
     ask the user to check.
4. **Check the page count before `pdf_split`.** A 200-page PDF split one page
   per file produces 200 files; look at the page count with `inspect_document`
   first and confirm the split with the user.
5. **The password stays in the conversation history.** When using
   `pdf_protect`, remind the user not to reuse a password from another system.
6. **Most tools take one file per call; `pdf_merge` and `images_to_pdf` take
   several in `files`.** Each item of `files` is `{"file_id": "f_..."}` or the
   file itself in `file_content`. When the user uploaded several files and each
   upload reference can only go into one `file_content`, first call
   `store_file` once per upload (reference in its `file_content`), collect the
   `file_id`s it returns, then make one call:
   `pdf_merge(files=[{"file_id": "f_A..."}, {"file_id": "f_B..."}])`, in the
   order the user wants. A client that can send HTTP may instead get an upload
   URL from `create_upload` and `PUT` the file to it. To run the same tool on
   several files, use `pipeline` (below) with the files in `files`.
7. **Always put the upload reference into `file_content`.**
   When you see a reference such as `[Uploaded Artifact: "report.pptx"]`, put it
   into `file_content`, not into `file_name` and not into `target_format`.
   For `file_name`, **do not send the parameter at all** (not even `null`); the
   host fills it in automatically. `target_format` holds only the target format
   (e.g. `"pdf"`).

8. **Give the user the download link.** A tool that produces a file lists
   each file's download link (and when it expires) in its text. If a file is
   marked "link only" it is not attached to the reply: give the user that
   link. The links stop working after the time shown.

9. **Pass a result on by its `file_id`.** To work further on a file a tool
   just returned (convert, then watermark, then compress), call the next tool
   with `file_id` set to that result's `file_id` instead of `file_content`;
   do not copy the file's content back. When the user gives a link to a file
   instead of uploading it, pass the link in `url`. Give only one of
   `file_content`, `file_id` and `url`. Only public addresses are fetched: if
   a URL is refused as internal or local, ask the user to upload the file.

10. **A long conversion answers with a `job_id`.** When a tool replies that
   the work is still running and gives a `job_id`, call `job_status` with that
   `job_id` (repeat while it says the work is still running) and give the user
   its result. Do not call the original tool again for the same file: that
   starts the conversion a second time. Use `job_cancel` if the user no longer
   wants the result, and `job_list` to find a `job_id` you lost.

11. **Use `pipeline` for more than one operation.** When the user wants
   several things done in a row (merge, then compress, then watermark; split,
   then rotate every part), make one `pipeline` call: the input (one file, or
   several in `files`) plus `steps`, each `{"tool": "<tool>", "args": {...}}`
   with that tool's own arguments but never the file. Intermediate files stay
   on the server and only the final file(s) come back. A one-file tool runs on
   each current file; `pdf_split` makes several; `pdf_merge` and
   `images_to_pdf` combine them into one. At most 10 steps; tools that answer
   with data (`extract_text`, `inspect_document`, `pdf_metadata`) are not
   steps: call them on the result. If the pipeline is refused, the message
   names the step to fix. For a single operation, call that tool directly.

12. **Always pass `output_name` to `convert_document`, set to the stem of the
   original file name.** When the user uploads
   `[Uploaded Artifact: "Vendor Review 2026.pptx"]`, `output_name` is
   `Vendor Review 2026` (no extension). Without it the user downloads a file
   called `converted.pdf` and cannot tell which document it is. If the user
   asked for a new name, use that instead.

## Quality expectations

- Office -> PDF: layout fully preserved; safe to use as is.
- Markdown / HTML -> Word / PDF: structure is correct; styling is the default template.
- PDF -> any editable format: **only text and images survive; the layout is rebuilt**.
  This is a limitation of the PDF format itself, not a defect of the service.
  Always let the user know before proceeding.

  In the output file, the PDF's embedded images are collected in an
  "Images from the original document" section at the end, labelled with the
  page they came from. Image coordinates in a PDF have no relation to the
  reading order of the text, so the original placement cannot be restored.
  **Tell the user this proactively**, so they know to move the images back to
  the right paragraphs instead of assuming the misplaced images are a
  conversion error.
