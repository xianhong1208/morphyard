FROM python:3.13-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---------------------------------------------------------------------------
# System dependencies of the conversion engines
#
# libreoffice-*  Only three components instead of the full libreoffice package,
#                saving about 400MB. writer=documents, calc=spreadsheets,
#                impress=presentations; drop one and its formats all break.
# pandoc         Text-format conversions.
# fonts-noto-cjk Required. Without it, CJK PDFs produced by LibreOffice render as
#                tofu boxes on every page, silently. The easiest thing to miss in
#                testing and the first thing to blow up in production.
# fonts-dejavu   Fallback for Latin text and symbols; also the Latin font that
#                pdf_watermark / pdf_page_numbers draw with.
# fonts-droid-fallback  The CJK font pdf_watermark / pdf_page_numbers draw with
#                (Apache-2.0). Noto CJK is OpenType/CFF, which reportlab cannot
#                embed; this one is TrueType, so the drawn text is embedded as a
#                subset. Without it Chinese falls back to a non-embedded CID font.
#
# This layer grows the image from ~200MB to ~1.2GB; that is the price of
# layout-faithful conversion.
# ---------------------------------------------------------------------------
# tesseract-ocr    OCR for images and scanned PDFs. chi-tra=Traditional Chinese,
#                  chi-sim=Simplified Chinese, eng=English. Without a language pack
#                  tesseract fails to start instead of degrading, so OCR dies.
# poppler-utils    Provides pdftoppm; scanned PDFs are rasterized before OCR.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        pandoc \
        tesseract-ocr \
        tesseract-ocr-chi-tra \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
        poppler-utils \
        fonts-noto-cjk \
        fonts-dejavu-core \
        fonts-droid-fallback \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --no-dev

COPY . .
RUN uv sync --no-dev

# The first LibreOffice start creates the user profile and takes a few seconds.
# Warm it up at build time so the first user request does not wait for nothing.
RUN soffice --headless --terminate_after_init >/dev/null 2>&1 || true

# Stored files and the download-link signing key. Keep the volume to keep links working
# across container re-creates; losing it only loses files that expire within the hour anyway.
VOLUME /app/data

EXPOSE 5055

# --no-sync: the image is already synced without the dev group. A plain `uv run`
# syncs the default groups, so every new container would download pytest and ruff
# before starting, or fail to start without network.
CMD ["uv", "run", "--no-sync", "python", "main.py", "--config", "config/config.yaml"]
