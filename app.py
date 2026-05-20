from __future__ import annotations

import base64
import binascii
import contextlib
import email.policy
import html
import io
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pdfplumber
import pymupdf
import pymupdf4llm
from pypdf import PdfReader, PdfWriter


HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8010"))
PREWARM_ON_STARTUP = os.environ.get("PREWARM_ON_STARTUP", "0").strip().lower() not in {"0", "false", "no"}
PROJECT_ROOT = Path(__file__).resolve().parent
STORAGE_DIR = PROJECT_ROOT / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
OUTPUTS_DIR = STORAGE_DIR / "outputs"
TMP_DIR = STORAGE_DIR / "tmp"
CONVERTER: Any | None = None
OCR_ENGINE: Any | None = None
CONVERTER_LOCK = threading.Lock()
OCR_ENGINE_LOCK = threading.Lock()
SUPPORTED_UPLOAD_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".csv",
    ".adoc",
    ".asciidoc",
    ".tex",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
    ".vtt",
}
UPLOAD_FIELD_NAMES = ("document", "file", "pdf")


class AppError(Exception):
    pass


def ensure_directories() -> None:
    for directory in (UPLOADS_DIR, OUTPUTS_DIR, TMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def configure_temp_directory() -> None:
    ensure_directories()
    os.environ.setdefault("TMPDIR", str(TMP_DIR))
    tempfile.tempdir = str(TMP_DIR)


def get_converter():
    global CONVERTER

    if CONVERTER is None:
        with CONVERTER_LOCK:
            if CONVERTER is None:
                configure_temp_directory()
                from docling.document_converter import DocumentConverter

                CONVERTER = DocumentConverter()

    return CONVERTER


def get_ocr_engine():
    global OCR_ENGINE

    if OCR_ENGINE is None:
        with OCR_ENGINE_LOCK:
            if OCR_ENGINE is None:
                from rapidocr import RapidOCR

                OCR_ENGINE = RapidOCR()

    return OCR_ENGINE


def sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    return cleaned or "upload.bin"


def markdown_title_from_filename(filename: str) -> str:
    stem = Path(filename or "upload").stem
    cleaned = stem.replace("_", " ").strip()
    return cleaned or "Document"


def is_pdf_path(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def save_upload_bytes(payload: bytes, filename: str) -> Path:
    original_name = sanitize_filename(Path(filename or "upload.bin").name)
    extension = Path(original_name).suffix.lower()

    if extension not in SUPPORTED_UPLOAD_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_UPLOAD_EXTENSIONS))
        raise AppError(f"Unsupported file type `{extension or '[none]'}`. Supported types: {supported}.")
    if not payload:
        raise AppError("The uploaded file is empty.")

    upload_name = f"{uuid4().hex}_{original_name}"
    upload_path = UPLOADS_DIR / upload_name
    upload_path.write_bytes(payload)
    return upload_path


def normalize_ocr_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if not mode:
        return "off"
    if mode in {"off", "false", "0", "no"}:
        return "off"
    if mode in {"selective", "on", "true", "1", "yes"}:
        return "selective"
    raise AppError("Invalid OCR mode. Use `off` or `selective`.")


def ocr_mode_uses_selective_ocr(value: str) -> bool:
    return normalize_ocr_mode(value) == "selective"


def render_page(
    *,
    markdown: str = "",
    error: str = "",
    output_name: str = "",
    pages_value: str = "",
    ocr_mode: str = "off",
) -> bytes:
    result_block = ""

    if error:
        result_block += f"""
        <section class="card error">
          <h2>Conversion Error</h2>
          <p>{html.escape(error)}</p>
        </section>
        """

    if markdown:
        escaped_markdown = html.escape(markdown)
        download_link = (
            f'<p><a href="/download?name={html.escape(output_name)}">Download markdown file</a></p>'
            if output_name
            else ""
        )
        result_block += f"""
        <section class="card result">
          <div class="result-head">
            <h2>Markdown Output</h2>
            <button id="copyButton" type="button" class="secondary-button">Copy Markdown</button>
          </div>
          {download_link}
          <textarea id="markdownOutput" readonly>{escaped_markdown}</textarea>
        </section>
        """

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Docling Document Reader</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f2e8;
      --panel: #fffdf8;
      --ink: #1d1b19;
      --muted: #6c655c;
      --accent: #9f3a23;
      --accent-2: #d97b28;
      --border: #d9c9b2;
      --error: #8f1d1d;
      --shadow: 0 18px 50px rgba(80, 45, 18, 0.12);
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(217, 123, 40, 0.18), transparent 28%),
        linear-gradient(180deg, #fff8ef 0%, var(--bg) 100%);
      min-height: 100vh;
    }}

    main {{
      max-width: 960px;
      margin: 0 auto;
      padding: 48px 20px 64px;
    }}

    .hero {{
      margin-bottom: 28px;
    }}

    h1 {{
      margin: 0 0 10px;
      font-size: clamp(2rem, 4vw, 3.5rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }}

    p {{
      margin: 0;
      color: var(--muted);
      font-size: 1.05rem;
      line-height: 1.6;
    }}

    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 24px;
      margin-bottom: 24px;
    }}

    form {{
      display: grid;
      gap: 18px;
    }}

    label {{
      display: block;
      font-weight: 700;
      margin-bottom: 8px;
    }}

    input[type="file"],
    input[type="password"],
    input[type="text"],
    select {{
      width: 100%;
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: #fff;
      font: inherit;
    }}

    button {{
      border: 0;
      border-radius: 999px;
      padding: 14px 24px;
      font: inherit;
      font-weight: 700;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #fffdf8;
      cursor: pointer;
      width: fit-content;
      transition: transform 160ms ease, opacity 160ms ease;
    }}

    button:disabled {{
      cursor: wait;
      opacity: 0.72;
      transform: none;
    }}

    .secondary-button {{
      background: #efe1cd;
      color: var(--ink);
      border: 1px solid var(--border);
      box-shadow: none;
      padding: 10px 16px;
    }}

    textarea {{
      width: 100%;
      min-height: 440px;
      resize: vertical;
      margin-top: 12px;
      padding: 18px;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: #fff;
      color: var(--ink);
      font: 0.95rem/1.5 "SFMono-Regular", "Menlo", monospace;
    }}

    a {{
      color: var(--accent);
      font-weight: 700;
      text-decoration: none;
    }}

    .error {{
      border-color: rgba(143, 29, 29, 0.25);
    }}

    .hint {{
      font-size: 0.95rem;
      margin-top: 6px;
    }}

    .progress-card {{
      display: none;
      align-items: center;
      gap: 16px;
      padding: 16px 18px;
      margin-bottom: 24px;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(255, 253, 248, 0.98), rgba(246, 242, 232, 0.96));
      box-shadow: var(--shadow);
    }}

    .progress-card.visible {{
      display: flex;
    }}

    .loading-spinner {{
      width: 42px;
      height: 42px;
      flex: 0 0 auto;
      border-radius: 50%;
      border: 4px solid rgba(159, 58, 35, 0.16);
      border-top-color: var(--accent);
      animation: spin 0.9s linear infinite;
    }}

    .loading-title {{
      margin: 0 0 4px;
      font-size: 1.05rem;
      font-weight: 700;
    }}

    .loading-copy {{
      margin: 0;
      color: var(--muted);
      font-size: 0.95rem;
    }}

    .result-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 10px;
    }}

    .result-head h2 {{
      margin: 0;
    }}

    @keyframes spin {{
      to {{
        transform: rotate(360deg);
      }}
    }}

    @media (max-width: 640px) {{
      main {{
        padding-top: 28px;
      }}

      .card {{
        padding: 18px;
        border-radius: 18px;
      }}

      .progress-card {{
        align-items: flex-start;
      }}

      .result-head {{
        align-items: stretch;
        flex-direction: column;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <div id="progressCard" class="progress-card" aria-hidden="true">
      <div class="loading-spinner"></div>
      <div>
        <p class="loading-title">Processing document</p>
        <p class="loading-copy">Running Docling and preparing markdown output. Large or scanned files may take longer.</p>
      </div>
    </div>
    <section class="hero">
      <h1>Docling Document Reader</h1>
      <p>Upload a supported document and get Markdown generated through Docling. PDF-only options stay available for locked files and page ranges.</p>
    </section>

    <section class="card">
      <form id="convertForm" action="/convert" method="post" enctype="multipart/form-data">
        <div>
          <label for="document">Document file</label>
          <input id="document" name="document" type="file" accept=".pdf,.docx,.pptx,.xlsx,.md,.markdown,.html,.htm,.csv,.adoc,.asciidoc,.tex,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp,.vtt" required>
          <p class="hint">Supported inputs include PDF, DOCX, PPTX, XLSX, Markdown, HTML, CSV, common image formats, and more.</p>
        </div>

        <div>
          <label for="password">PDF password (optional)</label>
          <input id="password" name="password" type="password" placeholder="Only needed for encrypted PDFs">
        </div>

        <div>
          <label for="pages">Pages to parse (PDF only)</label>
          <input id="pages" name="pages" type="text" placeholder="Examples: 1-3 or 2,5,7" value="{html.escape(pages_value)}">
          <p class="hint">Leave empty to parse the full document. Page selection applies only to PDFs.</p>
        </div>

        <div>
          <label for="ocr_mode">OCR mode (PDF only)</label>
          <select id="ocr_mode" name="ocr_mode">
            <option value="off"{" selected" if ocr_mode == "off" else ""}>Off</option>
            <option value="selective"{" selected" if ocr_mode == "selective" else ""}>Selective OCR</option>
          </select>
          <p class="hint">Selective OCR adds text from embedded images like logos, headers, and scanned banners, but can add noise on digital PDFs.</p>
        </div>

        <button id="submitButton" type="submit">Read with Docling</button>
      </form>
    </section>

    {result_block}
  </main>
  <script>
    const form = document.getElementById("convertForm");
    const progressCard = document.getElementById("progressCard");
    const submitButton = document.getElementById("submitButton");
    const copyButton = document.getElementById("copyButton");
    const markdownOutput = document.getElementById("markdownOutput");

    if (form && progressCard && submitButton) {{
      form.addEventListener("submit", () => {{
        progressCard.classList.add("visible");
        progressCard.setAttribute("aria-hidden", "false");
        submitButton.disabled = true;
        submitButton.textContent = "Processing...";
      }});
    }}

    if (copyButton && markdownOutput) {{
      copyButton.addEventListener("click", async () => {{
        const originalLabel = copyButton.textContent;
        try {{
          await navigator.clipboard.writeText(markdownOutput.value);
          copyButton.textContent = "Copied";
        }} catch (error) {{
          markdownOutput.select();
          document.execCommand("copy");
          copyButton.textContent = "Copied";
        }}

        setTimeout(() => {{
          copyButton.textContent = originalLabel;
        }}, 1400);
      }});
    }}
  </script>
</body>
</html>
"""
    return page.encode("utf-8")


def parse_page_selection(selection: str, total_pages: int) -> list[int]:
    if total_pages <= 0:
        raise AppError("The PDF does not contain any pages.")

    cleaned = selection.strip()
    if not cleaned:
        return list(range(1, total_pages + 1))

    pages: set[int] = set()
    for fragment in cleaned.split(","):
        part = fragment.strip()
        if not part:
            continue

        if "-" in part:
            start_text, end_text = (value.strip() for value in part.split("-", 1))
            if not start_text.isdigit() or not end_text.isdigit():
                raise AppError("Invalid pages value. Use formats like `1-3` or `2,5,7`.")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise AppError("Invalid pages value. Range start must be less than or equal to range end.")
            pages.update(range(start, end + 1))
            continue

        if not part.isdigit():
            raise AppError("Invalid pages value. Use formats like `1-3` or `2,5,7`.")
        pages.add(int(part))

    if not pages:
        raise AppError("No valid pages were selected.")

    invalid_pages = [page for page in sorted(pages) if page < 1 or page > total_pages]
    if invalid_pages:
        raise AppError(
            f"Selected pages are out of range. This PDF has {total_pages} pages."
        )

    return sorted(pages)


def select_pdf_pages(pdf_path: Path, selection: str) -> tuple[Path, list[int], int]:
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    selected_pages = parse_page_selection(selection, total_pages)

    if len(selected_pages) == total_pages:
        return pdf_path, selected_pages, total_pages

    subset_path = pdf_path.with_name(f"{pdf_path.stem}_pages_{uuid4().hex[:8]}.pdf")
    writer = PdfWriter()

    for page_number in selected_pages:
        writer.add_page(reader.pages[page_number - 1])

    if reader.metadata:
        metadata = {
            key: str(value)
            for key, value in reader.metadata.items()
            if key and value is not None
        }
        if metadata:
            writer.add_metadata(metadata)

    with subset_path.open("wb") as handle:
        writer.write(handle)

    return subset_path, selected_pages, total_pages


def parse_multipart_form(
    content_type: str, body: bytes
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    message = BytesParser(policy=email.policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode("utf-8")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )

    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}

    if not message.is_multipart():
        return fields, files

    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue

        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""

        if filename:
            files[name] = (filename, payload)
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")

    return fields, files
def unlock_pdf_if_needed(source_path: Path, password: str) -> Path:
    reader = PdfReader(str(source_path))
    if not reader.is_encrypted:
        return source_path

    if not password:
        raise AppError("This PDF is encrypted. Provide its password and try again.")

    if reader.decrypt(password) == 0:
        raise AppError("The provided PDF password is incorrect.")

    unlocked_path = source_path.with_name(f"{source_path.stem}_unlocked.pdf")
    writer = PdfWriter()

    for page in reader.pages:
        writer.add_page(page)

    if reader.metadata:
        metadata = {
            key: str(value)
            for key, value in reader.metadata.items()
            if key and value is not None
        }
        if metadata:
            writer.add_metadata(metadata)

    with unlocked_path.open("wb") as handle:
        writer.write(handle)

    return unlocked_path


def normalize_extracted_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("©", "'")
    text = text.replace("Á", "Rs ")
    text = re.sub(r"(?<![A-Za-z0-9])C\s*(?=\d[\d,]*\.\d{2}\b)", "Rs ", text)
    lines: list[str] = []
    previous_blank = False

    for raw_line in text.splitlines():
        raw_line = re.sub(r"\(cid:\d+\)", " ", raw_line)
        raw_line = "".join(ch for ch in raw_line if ch == "\t" or ch == "\n" or ord(ch) >= 32)
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line.startswith("l "):
            line = f"- {line[2:].strip()}"
        if not line:
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue

        lines.append(line)
        previous_blank = False

    return "\n".join(lines).strip()


def normalize_text_lines(text: str) -> list[str]:
    normalized = normalize_extracted_text(text)
    return [line for line in normalized.splitlines() if line]


def looks_like_numeric_value(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.fullmatch(r"[+-=._]+", stripped):
        return True
    if is_amount_token(stripped):
        return True
    if re.fullmatch(r"[A-Z]?\d[\d,./-]*", stripped):
        return True
    if re.fullmatch(r"\d{1,2}\s+[A-Za-z]{3},?\s+\d{4}", stripped):
        return True
    return False


def is_probable_label(line: str) -> bool:
    stripped = line.strip()
    if not stripped or looks_like_numeric_value(stripped):
        return False
    if len(stripped) > 70:
        return False
    letters = sum(ch.isalpha() for ch in stripped)
    return letters >= 3


def is_noise_line(line: str) -> bool:
    if not line:
        return False
    if "(cid:" in line:
        return True
    weird_chars = sum(1 for ch in line if ord(ch) < 32 or ord(ch) > 126)
    if weird_chars > 0 and weird_chars / max(len(line), 1) > 0.15:
        return True
    return False


def is_useful_ocr_line(line: str) -> bool:
    if not line or is_noise_line(line):
        return False
    if len(line) < 3 or len(line) > 220:
        return False
    if not re.search(r"[A-Za-z0-9]", line):
        return False
    weird_chars = sum(1 for ch in line if ord(ch) > 126)
    if weird_chars / max(len(line), 1) > 0.1:
        return False
    return True


def has_significant_image_blocks(page: pymupdf.Page) -> bool:
    page_area = page.rect.width * page.rect.height
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 1:
            continue
        x0, y0, x1, y1 = block["bbox"]
        block_area = max(x1 - x0, 0) * max(y1 - y0, 0)
        if page_area <= 0:
            continue
        area_ratio = block_area / page_area
        if area_ratio >= 0.015 or ((y1 - y0) >= 70 and y0 < page.rect.height * 0.45):
            return True
    return False


def should_ocr_image_block(page: pymupdf.Page, bbox: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = bbox
    width = max(x1 - x0, 0)
    height = max(y1 - y0, 0)
    if width < 36 or height < 18:
        return False

    page_area = max(page.rect.width * page.rect.height, 1)
    block_area = width * height
    area_ratio = block_area / page_area

    if area_ratio >= 0.003:
        return True
    if y0 <= page.rect.height * 0.28 and width >= 80:
        return True
    return height >= 28 and width >= 120


def normalize_ocr_text_line(text: str) -> str:
    text = normalize_extracted_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compact_ocr_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def ocr_lines_from_page_region(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    seen: set[str],
    *,
    scale: float = 3,
    min_score: float = 0.45,
) -> list[str]:
    ocr = get_ocr_engine()
    group_lines: list[str] = []

    try:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False)
        ocr_output = ocr(pixmap.tobytes("png"))
    except Exception:
        return group_lines

    texts = getattr(ocr_output, "txts", ()) or ()
    scores = getattr(ocr_output, "scores", ()) or ()
    for index, raw_text in enumerate(texts):
        score = float(scores[index]) if index < len(scores) else 1.0
        if score < min_score:
            continue
        line = normalize_ocr_text_line(str(raw_text))
        if not is_useful_ocr_line(line):
            continue
        folded = compact_ocr_key(line)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        group_lines.append(line)

    return group_lines


def is_probable_header_brand_line(line: str) -> bool:
    if not is_useful_ocr_line(line) or len(line) > 60 or looks_like_numeric_value(line):
        return False

    compact = compact_ocr_key(line)
    if not compact:
        return False

    brand_tokens = {
        "axis",
        "bank",
        "hdfc",
        "icici",
        "yesbank",
        "sbi",
        "kotak",
        "indusind",
        "idfc",
        "rbl",
        "hsbc",
        "amex",
        "americanexpress",
        "standardchartered",
        "federal",
        "canara",
        "baroda",
        "pnb",
    }
    if any(token in compact for token in brand_tokens):
        return True

    return False


def extract_rendered_header_ocr_group(page: pymupdf.Page, seen: set[str]) -> list[str]:
    header_height = min(90.0, page.rect.height * 0.12)
    candidate_regions = [
        pymupdf.Rect(0, 0, page.rect.width * 0.42, header_height),
        pymupdf.Rect(page.rect.width * 0.58, 0, page.rect.width, header_height),
    ]

    header_lines: list[str] = []
    for rect in candidate_regions:
        for line in ocr_lines_from_page_region(page, rect, seen, scale=4, min_score=0.5):
            if is_probable_header_brand_line(line):
                header_lines.append(line)

    return header_lines[:4]


def extract_grouped_text_from_image_blocks(page: pymupdf.Page) -> list[list[str]]:
    candidate_rects: list[pymupdf.Rect] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 1:
            continue
        bbox = tuple(float(value) for value in block.get("bbox", (0, 0, 0, 0)))
        if not should_ocr_image_block(page, bbox):
            continue
        candidate_rects.append(pymupdf.Rect(*bbox))

    candidate_rects.sort(key=lambda rect: (rect.y0, -(rect.width * rect.height)))
    seen: set[str] = set()
    groups: list[list[str]] = []

    for rect in candidate_rects[:6]:
        group_lines = ocr_lines_from_page_region(page, rect, seen)
        if group_lines:
            groups.append(group_lines)

    header_group = extract_rendered_header_ocr_group(page, seen)
    if header_group:
        groups.insert(0, header_group)

    return groups


def extract_text_from_image_blocks(page: pymupdf.Page) -> list[str]:
    return [line for group in extract_grouped_text_from_image_blocks(page) for line in group]


def collect_page_image_ocr_groups(
    pdf_path: Path,
    page_labels: list[int] | None = None,
) -> dict[int, list[list[str]]]:
    document = pymupdf.open(str(pdf_path))
    page_sections: dict[int, list[list[str]]] = {}

    for page_index in range(document.page_count):
        page = document[page_index]
        groups = extract_grouped_text_from_image_blocks(page)
        if not groups:
            continue
        page_label = page_labels[page_index] if page_labels and page_index < len(page_labels) else page_index + 1
        page_sections[page_label] = groups

    return page_sections


def render_inline_image_ocr_block(lines: list[str]) -> str:
    return "\n".join(
        [
            "### Image OCR",
            "",
            *(f"- {line}" for line in lines),
        ]
    )


def remove_image_placeholders(markdown: str) -> str:
    cleaned = re.sub(r"\n?<!-- image -->\n?", "\n", markdown)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def dedupe_image_lines_for_markdown(section_markdown: str, lines: list[str]) -> list[str]:
    existing_lines = normalize_text_lines(section_markdown)
    existing_folded = {line.casefold() for line in existing_lines}
    unique_lines: list[str] = []

    for line in lines:
        folded = line.casefold()
        if folded in existing_folded:
            continue
        if any(folded in existing.casefold() or existing.casefold() in folded for existing in existing_lines):
            continue
        if any(folded == prior.casefold() for prior in unique_lines):
            continue
        unique_lines.append(line)

    return unique_lines


def append_image_ocr_sections(
    markdown: str,
    pdf_path: Path,
    page_labels: list[int] | None = None,
) -> str:
    page_sections = collect_page_image_ocr_groups(pdf_path, page_labels)
    if not page_sections:
        return remove_image_placeholders(markdown)

    parts = markdown.strip().split("\n\n---\n\n")
    merged_parts: list[str] = []
    consumed_labels: set[int] = set()

    for part in parts:
        match = re.search(r"^## Page (\d+)\s*$", part, flags=re.M)
        if not match:
            merged_parts.append(part)
            continue

        page_label = int(match.group(1))
        image_groups = page_sections.get(page_label, [])
        if not image_groups:
            merged_parts.append(part)
            continue

        placeholder_count = part.count("<!-- image -->")
        remaining_groups: list[list[str]] = []
        page_text = part

        if placeholder_count > 0:
            for image_group in image_groups:
                unique_lines = dedupe_image_lines_for_markdown(page_text, image_group)
                if unique_lines:
                    remaining_groups.append(unique_lines)

            for unique_lines in remaining_groups[:placeholder_count]:
                page_text = page_text.replace(
                    "<!-- image -->",
                    render_inline_image_ocr_block(unique_lines),
                    1,
                )

            remaining_groups = remaining_groups[placeholder_count:]
        else:
            for image_group in image_groups:
                unique_lines = dedupe_image_lines_for_markdown(page_text, image_group)
                if unique_lines:
                    remaining_groups.append(unique_lines)

        if not remaining_groups:
            merged_parts.append(page_text)
            consumed_labels.add(page_label)
            continue

        image_blocks = []
        for unique_lines in remaining_groups:
            image_blocks.append(
                "\n".join(
                    [
                        "### Image OCR",
                        "",
                        *(f"- {line}" for line in unique_lines),
                    ]
                )
            )
        merged_parts.append(f"{page_text.rstrip()}\n\n" + "\n\n".join(image_blocks))
        consumed_labels.add(page_label)

    leftover_sections: list[str] = []
    for page_label, groups in page_sections.items():
        if page_label in consumed_labels:
            continue
        rendered_groups: list[str] = []
        for group in groups:
            rendered_groups.append(
                "\n".join(
                    [
                        "### Image OCR",
                        "",
                        *(f"- {line}" for line in group),
                    ]
                )
            )
        leftover_sections.append(
            "\n\n".join([f"### Page {page_label} image text", *rendered_groups])
        )

    if leftover_sections:
        merged_parts.append("\n\n".join(["## Image OCR", *leftover_sections]))

    merged_markdown = "\n\n---\n\n".join(part.strip() for part in merged_parts if part.strip()).strip()
    return remove_image_placeholders(merged_markdown)


def extract_sorted_page_text(page: pymupdf.Page, *, include_selective_ocr: bool = True) -> str:
    blocks = page.get_text("blocks", sort=True)
    block_texts: list[str] = []

    for block in blocks:
        if len(block) < 5:
            continue
        raw_text = str(block[4] or "")
        lines = [line for line in normalize_text_lines(raw_text) if not is_noise_line(line)]
        if not lines:
            continue
        block_texts.append("\n".join(lines))

    base_text = "\n\n".join(block_texts).strip()
    if not include_selective_ocr or not has_significant_image_blocks(page):
        return base_text

    supplement_lines: list[str] = []
    existing = {line.casefold() for line in normalize_text_lines(base_text)}

    for line in extract_text_from_image_blocks(page):
        folded = line.casefold()
        if folded in existing:
            continue
        supplement_lines.append(line)
        existing.add(folded)

    try:
        ocr_textpage = page.get_textpage_ocr(flags=0, language="eng", dpi=150, full=False)
        ocr_text = page.get_text("text", textpage=ocr_textpage)
    except Exception:
        if not supplement_lines:
            return base_text
        supplement = "\n".join(supplement_lines)
        return "\n\n".join(part for part in [base_text, supplement] if part).strip()

    for line in normalize_text_lines(ocr_text):
        if not is_useful_ocr_line(line):
            continue
        folded = line.casefold()
        if folded in existing:
            continue
        if any(folded in prior.casefold() or prior.casefold() in folded for prior in supplement_lines):
            continue
        supplement_lines.append(line)
        if len(supplement_lines) >= 12:
            break

    if not supplement_lines:
        return base_text

    supplement = "\n".join(supplement_lines)
    return "\n\n".join(part for part in [base_text, supplement] if part).strip()


def collapse_continuation_lines(lines: list[str]) -> list[str]:
    collapsed: list[str] = []
    for line in lines:
        if (
            collapsed
            and not looks_like_numeric_value(line)
            and (
                line.startswith("(")
                or line[0].islower()
                or (
                    len(line) <= 24
                    and not re.search(r"\d", line)
                    and collapsed[-1].upper() == collapsed[-1]
                )
            )
        ):
            collapsed[-1] = f"{collapsed[-1]} {line}".strip()
            continue
        collapsed.append(line)
    return collapsed


def mostly_numeric_lines(lines: list[str]) -> bool:
    if not lines:
        return False
    numeric_like = sum(1 for line in lines if looks_like_numeric_value(line))
    return numeric_like >= max(1, len(lines) - 1)


def mostly_label_lines(lines: list[str]) -> bool:
    if not lines:
        return False
    label_like = sum(1 for line in lines if is_probable_label(line))
    return label_like >= max(1, len(lines) - 1)


def collect_text_blocks(page: pymupdf.Page) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for raw_block in page.get_text("blocks", sort=True):
        if len(raw_block) < 5:
            continue
        raw_text = str(raw_block[4] or "")
        lines = [line for line in normalize_text_lines(raw_text) if not is_noise_line(line)]
        if not lines:
            continue
        lines = collapse_continuation_lines(lines)
        x0, y0, x1, y1 = raw_block[:4]
        blocks.append(
            {
                "x0": float(x0),
                "y0": float(y0),
                "x1": float(x1),
                "y1": float(y1),
                "lines": lines,
                "text": "\n".join(lines),
            }
        )
    return blocks


def looks_like_footer_block(block: dict[str, Any], page_height: float) -> bool:
    text = block["text"]
    lowered = text.lower()
    if re.search(r"page\s+\d+\s+of\s+\d+", lowered):
        return True
    if "digitally" in lowered or "signed by" in lowered:
        return True
    if block["y0"] >= page_height * 0.9 and ("bank limited" in lowered or lowered == "useful links"):
        return True
    return False


def extract_inline_pairs(lines: list[str]) -> list[tuple[str, str]]:
    if len(lines) == 2 and is_probable_label(lines[0]) and looks_like_numeric_value(lines[1]):
        return [(lines[0], normalize_amount_token(lines[1]) if is_amount_token(lines[1]) else lines[1])]
    if len(lines) == 3 and is_probable_label(lines[0]) and is_probable_label(lines[1]) and looks_like_numeric_value(lines[2]):
        value = normalize_amount_token(lines[2]) if is_amount_token(lines[2]) else lines[2]
        return [(f"{lines[0]} {lines[1]}", value)]
    return []


def looks_like_value_line(line: str) -> bool:
    stripped = line.strip()
    return (
        looks_like_numeric_value(stripped)
        or bool(re.fullmatch(r"\d{1,2}\s+[A-Za-z]{3},?\s+\d{4}", stripped))
        or bool(re.fullmatch(r"\d{1,2}\s+[A-Za-z]+\s*,\s*\d{4}", stripped))
    )


def contains_transaction_marker(lines: list[str]) -> bool:
    return any(
        re.search(r"\d{2}/\d{2}/\d{4}", line)
        or "transaction" in line.lower()
        or "serno" in line.lower()
        for line in lines
    )


def render_key_value_pairs(title: str, pairs: list[tuple[str, str]]) -> str:
    table_rows = [[label, value] for label, value in pairs]
    return "\n".join([f"### {title}", "", *render_markdown_table(["Field", "Value"], table_rows)])


def build_structured_block_sections(page: pymupdf.Page) -> list[str]:
    blocks = collect_text_blocks(page)
    if not blocks:
        return []

    sections: list[str] = []
    consumed: set[int] = set()
    page_height = float(page.rect.height)

    for index, block in enumerate(blocks):
        if index in consumed or looks_like_footer_block(block, page_height):
            continue

        lines = block["lines"]
        if contains_transaction_marker(lines):
            continue

        if index + 1 < len(blocks) and index + 1 not in consumed:
            next_block = blocks[index + 1]
            next_lines = next_block["lines"]
            if (
                len(lines) == 1
                and len(next_lines) == 1
                and is_probable_label(lines[0])
                and looks_like_value_line(next_lines[0])
                and abs(block["x0"] - next_block["x0"]) <= 90
                and next_block["y0"] <= block["y1"] + 30
            ):
                sections.append(
                    render_key_value_pairs(
                        "Key Figures",
                        [(lines[0], normalize_amount_token(next_lines[0]) if is_amount_token(next_lines[0]) else next_lines[0])],
                    )
                )
                consumed.update({index, index + 1})
                continue

        inline_run: list[tuple[str, str]] = []
        run_end = index
        while run_end < len(blocks) and run_end not in consumed:
            candidate = blocks[run_end]
            if looks_like_footer_block(candidate, page_height):
                break
            if run_end > index and candidate["y0"] - blocks[run_end - 1]["y1"] > 24:
                break
            pairs = extract_inline_pairs(candidate["lines"])
            if any(pair[0].strip().lower() in {"l", "pi"} for pair in pairs):
                break
            if contains_transaction_marker(candidate["lines"]):
                break
            if not pairs:
                break
            inline_run.extend(pairs)
            run_end += 1

        if len(inline_run) >= 2:
            sections.append(render_key_value_pairs(f"Structured Section {len(sections) + 1}", inline_run))
            consumed.update(range(index, run_end))
            continue

        if (
            len(lines) == 1
            and re.search(r"\bSR\s+NO\.", lines[0], flags=re.I)
            and "transaction" in lines[0].lower()
            and "amount" in lines[0].lower()
            and index + 2 < len(blocks)
        ):
            data_lines = blocks[index + 1]["lines"]
            total_lines = blocks[index + 2]["lines"]
            if len(data_lines) == 3 and len(total_lines) == 2:
                sections.append(
                    "\n".join(
                        [
                            "### Cash Back Summary",
                            "",
                            *render_markdown_table(
                                ["SR NO.", "Transaction", "Amount"],
                                [[data_lines[0], data_lines[1], data_lines[2]], ["Total", "", total_lines[1]]],
                            ),
                        ]
                    )
                )
                consumed.update({index, index + 1, index + 2})
                continue

        if (
            len(lines) >= 4
            and "gst entry" in " ".join(lines).lower()
            and index + 1 < len(blocks)
            and len(blocks[index + 1]["lines"]) >= 4
        ):
            headers = lines[:4]
            values = blocks[index + 1]["lines"][:4]
            sections.append(
                "\n".join(
                    [
                        "### GST Summary",
                        "",
                        *render_markdown_table(headers, [values]),
                    ]
                )
            )
            consumed.update({index, index + 1})

    return sections


def looks_like_account_marker(line: str) -> bool:
    return bool(re.fullmatch(r"\d{4,}X{2,}\d{2,}", line))


def looks_like_transaction_header(line: str) -> bool:
    lowered = line.lower()
    return "date" in lowered and "transaction" in lowered and "amount" in lowered


def looks_like_transaction_header_window(lines: list[str], start: int) -> bool:
    window = " ".join(line.lower() for line in lines[start : start + 8] if line)
    if not window:
        return False

    return (
        "date" in window
        and "transaction" in window
        and "amount" in window
        and ("serno" in window or "description" in window or "reward" in window)
    )


def looks_like_datetime_transaction_header_window(lines: list[str], start: int) -> bool:
    window = " ".join(line.lower() for line in lines[start : start + 8] if line)
    local_window = " ".join(line.lower() for line in lines[start : start + 3] if line)
    if not window:
        return False

    return (
        "date" in local_window
        and ("time" in local_window or "& time" in local_window)
        and "transaction" in window
        and "amount" in window
    )


def parse_statement_transaction_row(line: str) -> list[str] | None:
    tokens = line.split()
    if len(tokens) < 4:
        return None
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", tokens[0]):
        return None

    index = 1
    ser_no = ""
    if index < len(tokens) and re.fullmatch(r"\d{8,}", tokens[index]):
        ser_no = tokens[index]
        index += 1

    tail_index = len(tokens)
    amount = ""
    if tail_index >= 2 and tokens[-1] in {"CR", "DR"} and re.fullmatch(r"[\d,]+\.\d{2}", tokens[-2]):
        amount = f"{tokens[-2]} {tokens[-1]}"
        tail_index -= 2
    elif re.fullmatch(r"[\d,]+\.\d{2}", tokens[-1]):
        amount = tokens[-1]
        tail_index -= 1
    else:
        return None

    reward = ""
    if tail_index - 1 >= index and re.fullmatch(r"\d+", tokens[tail_index - 1]):
        reward = tokens[tail_index - 1]
        tail_index -= 1

    description = " ".join(tokens[index:tail_index]).strip()
    if not description:
        return None

    amount = amount.replace("Á", "Rs ")
    if not amount.startswith("Rs "):
        amount = f"Rs {amount}"

    return [tokens[0], ser_no, description, reward, amount]


def is_amount_token(value: str) -> bool:
    normalized = value.replace("`", "Rs ").replace("Á", "Rs ").strip()
    normalized = re.sub(r"^[+-]\s*", "", normalized)
    parts = normalized.split()
    if len(parts) == 2 and parts[1] in {"CR", "DR"}:
        return bool(re.fullmatch(r"(?:Rs\s+)?[\d,]+\.\d{2}", parts[0]))
    return bool(re.fullmatch(r"(?:Rs\s+)?[\d,]+\.\d{2}", normalized))


def normalize_amount_token(value: str) -> str:
    normalized = value.replace("`", "Rs ").replace("Á", "Rs ").strip()
    prefix = ""
    if normalized.startswith("+"):
        prefix = "+ "
        normalized = normalized[1:].strip()
    elif normalized.startswith("-"):
        prefix = "- "
        normalized = normalized[1:].strip()

    if not normalized.startswith("Rs "):
        normalized = f"Rs {normalized}"
    normalized = prefix + normalized
    return normalized


def parse_statement_transaction_row_multiline(
    lines: list[str], start_index: int
) -> tuple[list[str], int] | None:
    if start_index + 2 >= len(lines):
        return None
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", lines[start_index]):
        return None
    if not re.fullmatch(r"\d{8,}", lines[start_index + 1]):
        return None

    index = start_index + 2
    description_lines: list[str] = []

    while index < len(lines):
        current = lines[index]
        if not current:
            index += 1
            continue
        if looks_like_account_marker(current):
            break
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", current):
            break
        if re.fullmatch(r"\d+", current) and index + 1 < len(lines) and is_amount_token(lines[index + 1]):
            break
        if is_amount_token(current):
            break
        description_lines.append(current)
        index += 1

    if not description_lines or index >= len(lines):
        return None

    reward = ""
    if re.fullmatch(r"\d+", lines[index]):
        reward = lines[index]
        index += 1

    if index >= len(lines) or not is_amount_token(lines[index]):
        return None

    amount = normalize_amount_token(lines[index])
    row = [
        lines[start_index],
        lines[start_index + 1],
        " ".join(description_lines).strip(),
        reward,
        amount,
    ]
    return row, index + 1


def parse_datetime_transaction_row_multiline(
    lines: list[str], start_index: int
) -> tuple[list[str], int] | None:
    if start_index >= len(lines):
        return None

    match = re.fullmatch(r"(\d{2}/\d{2}/\d{4})\|\s*(\d{2}:\d{2})", lines[start_index])
    if not match:
        return None

    index = start_index + 1
    description_lines: list[str] = []

    while index < len(lines):
        current = lines[index]
        if not current:
            index += 1
            continue
        if re.fullmatch(r"(\d{2}/\d{2}/\d{4})\|\s*(\d{2}:\d{2})", current):
            break
        if is_amount_token(current):
            break
        description_lines.append(current)
        index += 1

    if not description_lines or index >= len(lines) or not is_amount_token(lines[index]):
        return None

    amount = normalize_amount_token(lines[index])
    index += 1

    indicator = ""
    if index < len(lines) and lines[index] and not re.fullmatch(r"(\d{2}/\d{2}/\d{4})\|\s*(\d{2}:\d{2})", lines[index]):
        if len(lines[index]) <= 24:
            indicator = lines[index]
            index += 1

    tx_date, tx_time = match.groups()
    row = [f"{tx_date} {tx_time}", " ".join(description_lines).strip(), amount, indicator]
    return row, index


def render_transaction_sections(
    sections: list[tuple[str | None, list[list[str]]]], heading: str = "### Transactions"
) -> list[str]:
    if not sections:
        return []

    lines = [heading, ""]
    for section_index, (account, account_rows) in enumerate(sections):
        if section_index > 0:
            lines.append("")
        if account:
            lines.append(f"Account: {account}")
            lines.append("")
        lines.extend(
            render_markdown_table(
                ["Date", "SerNo.", "Transaction Details", "Reward", "Amount"],
                account_rows,
            )
        )

    return lines


def extract_transaction_sections_from_text(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip() and not is_noise_line(line)]
    output: list[str] = []
    index = 0

    while index < len(lines):
        header_index = index
        if not looks_like_transaction_header_window(lines, index):
            index += 1
            continue

        while index < len(lines):
            current = lines[index]
            lowered = current.lower()
            parsed_row = parse_statement_transaction_row(current)
            if looks_like_account_marker(current) or parsed_row is not None:
                break
            if any(
                token in lowered
                for token in ("date", "serno", "transaction", "reward", "points", "intl", "amount")
            ):
                index += 1
                continue
            break

        sections: list[tuple[str | None, list[list[str]]]] = []
        current_account: str | None = None
        rows: list[list[str]] = []

        while index < len(lines):
            current = lines[index]
            if not current:
                index += 1
                continue
            lowered = current.lower()
            if looks_like_account_marker(current):
                if rows:
                    sections.append((current_account, rows))
                    rows = []
                current_account = current
                index += 1
                continue
            if re.fullmatch(r"\d+%", current) or lowered.startswith("others-") or lowered == "spends overview":
                index += 1
                continue

            parsed_row = parse_statement_transaction_row(current)
            if parsed_row is not None:
                rows.append(parsed_row)
                index += 1
                continue

            parsed_multiline = parse_statement_transaction_row_multiline(lines, index)
            if parsed_multiline is not None:
                row, next_index = parsed_multiline
                rows.append(row)
                index = next_index
                continue

            break

        if rows:
            sections.append((current_account, rows))

        if sections:
            return render_transaction_sections(sections)

        index = header_index + 1

    return output


def extract_datetime_transaction_sections_from_text(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip() and not is_noise_line(line)]
    index = 0

    while index < len(lines):
        header_index = index
        if not looks_like_datetime_transaction_header_window(lines, index):
            index += 1
            continue

        skipped_context = 0
        while index < len(lines):
            current = lines[index]
            lowered = current.lower()
            if re.fullmatch(r"(\d{2}/\d{2}/\d{4})\|\s*(\d{2}:\d{2})", current):
                break
            if any(token in lowered for token in ("date", "time", "transaction", "amount", "description", "pi")):
                index += 1
                continue
            if skipped_context < 6:
                skipped_context += 1
                index += 1
                continue
            break

        rows: list[list[str]] = []
        while index < len(lines):
            if not lines[index]:
                index += 1
                continue
            parsed = parse_datetime_transaction_row_multiline(lines, index)
            if parsed is None:
                break
            row, next_index = parsed
            rows.append(row)
            index = next_index

        if rows:
            return [
                "### Transactions",
                "",
                *render_markdown_table(
                    ["Date & Time", "Transaction Description", "Amount", "PI"],
                    rows,
                ),
            ]

        index = header_index + 1

    return []


def skip_statement_transaction_block(lines: list[str], index: int) -> int:
    index += 1
    while index < len(lines):
        current = lines[index]
        lowered = current.lower()
        if not current:
            index += 1
            continue
        parsed_multiline = parse_statement_transaction_row_multiline(lines, index)
        if parsed_multiline is not None:
            _, next_index = parsed_multiline
            index = next_index
            continue
        if looks_like_account_marker(current) or parse_statement_transaction_row(current) is not None:
            index += 1
            continue
        if re.fullmatch(r"\d+%", current) or lowered.startswith("others-") or lowered == "spends overview":
            index += 1
            continue
        if any(
            token in lowered
            for token in ("date", "serno", "transaction", "reward", "points", "intl", "amount")
        ):
            index += 1
            continue
        break
    return index


def skip_datetime_transaction_block(lines: list[str], index: int) -> int:
    index += 1
    skipped_context = 0
    while index < len(lines):
        current = lines[index]
        lowered = current.lower()
        if not current:
            index += 1
            continue
        if re.fullmatch(r"(\d{2}/\d{2}/\d{4})\|\s*(\d{2}:\d{2})", current):
            break
        if any(token in lowered for token in ("date", "time", "transaction", "amount", "description", "pi")):
            index += 1
            continue
        if skipped_context < 6:
            skipped_context += 1
            index += 1
            continue
        break

    while index < len(lines):
        current = lines[index]
        lowered = current.lower()
        if not current:
            index += 1
            continue
        if index + 1 < len(lines) and parse_datetime_transaction_row_multiline(lines, index + 1) is not None:
            index += 1
            continue
        parsed = parse_datetime_transaction_row_multiline(lines, index)
        if parsed is not None:
            _, next_index = parsed
            index = next_index
            continue
        if any(token in lowered for token in ("date", "time", "transaction", "amount", "description", "pi")):
            index += 1
            continue
        break
    return index


def next_non_empty_line_index(lines: list[str], start: int) -> int | None:
    index = start
    while index < len(lines):
        if lines[index].strip():
            return index
        index += 1
    return None


def format_generic_text_page(text: str) -> str:
    lines = [line for line in text.splitlines() if not is_noise_line(line)]
    output: list[str] = []
    index = 0
    emitted_transactions = False

    while index < len(lines):
        line = lines[index]
        lowered = line.lower()

        if re.fullmatch(r"page\s+\d+\s+of\s+\d+", line, flags=re.I):
            index += 1
            continue
        if lowered in {"digitally", "signed by", "date:"}:
            index += 1
            continue
        if line == "l":
            next_index = next_non_empty_line_index(lines, index + 1)
            if next_index is not None and lines[next_index] != "l":
                bullet_parts = [lines[next_index]]
                follow_index = next_index + 1
                while follow_index < len(lines):
                    candidate = lines[follow_index]
                    if not candidate.strip():
                        break
                    if candidate == "l":
                        break
                    bullet_parts.append(candidate)
                    follow_index += 1
                bullet_text = " ".join(part.strip() for part in bullet_parts if part.strip())
                if bullet_text.startswith("- "):
                    trailing_index = next_non_empty_line_index(lines, follow_index)
                    if (
                        trailing_index is not None
                        and trailing_index == follow_index + 1
                        and len(lines[trailing_index]) <= 12
                        and re.search(r"\d", lines[trailing_index])
                    ):
                        output.append(f"{bullet_text} {lines[trailing_index]}")
                        index = trailing_index + 1
                        continue
                    output.append(bullet_text)
                else:
                    output.append(f"- {bullet_text}")
                index = follow_index
                continue
            index += 1
            continue
        if lowered == "cash back summary":
            idx1 = next_non_empty_line_index(lines, index + 1)
            idx2 = next_non_empty_line_index(lines, (idx1 or index) + 1) if idx1 is not None else None
            idx3 = next_non_empty_line_index(lines, (idx2 or index) + 1) if idx2 is not None else None
            idx4 = next_non_empty_line_index(lines, (idx3 or index) + 1) if idx3 is not None else None
            idx5 = next_non_empty_line_index(lines, (idx4 or index) + 1) if idx4 is not None else None
            idx6 = next_non_empty_line_index(lines, (idx5 or index) + 1) if idx5 is not None else None
            idx7 = next_non_empty_line_index(lines, (idx6 or index) + 1) if idx6 is not None else None
            idx8 = next_non_empty_line_index(lines, (idx7 or index) + 1) if idx7 is not None else None
            if (
                idx1 is not None
                and idx2 is not None
                and idx3 is not None
                and idx4 is not None
                and idx5 is not None
                and idx6 is not None
                and idx7 is not None
                and idx8 is not None
                and lines[idx1].upper() == "SR NO."
                and lines[idx2].upper() == "TRANSACTION"
                and lines[idx3].upper() == "AMOUNT"
            ):
                output.append("### Cash Back Summary")
                output.append("")
                output.extend(
                    render_markdown_table(
                        ["SR NO.", "Transaction", "Amount"],
                        [[lines[idx4], lines[idx5], lines[idx6]], ["Total", "", lines[idx8]]],
                    )
                )
                output.append("")
                index = idx8 + 1
                continue

        if (
            line == "GST Entry"
            and (header_start := next_non_empty_line_index(lines, index + 1)) is not None
            and header_start + 12 < len(lines)
        ):
            compact = [entry for entry in lines[header_start : header_start + 13] if entry.strip()]
            if len(compact) >= 12 and compact[1] == "Type" and compact[4] == "Rate %":
                output.append("### GST Summary")
                output.append("")
                output.extend(
                    render_markdown_table(
                        ["GST Entry", "GST Type", "Invoice Number", "GST Rate %", "State Code"],
                        [[compact[7], compact[8], compact[9], compact[10], compact[11]]],
                    )
                )
                output.append("")
                index = header_start + 13
                continue

        if (
            lowered == "cash back summary"
            and index + 8 < len(lines)
            and lines[index + 1].upper() == "SR NO."
            and lines[index + 2].upper() == "TRANSACTION"
            and lines[index + 3].upper() == "AMOUNT"
        ):
            output.append("### Cash Back Summary")
            output.append("")
            output.extend(
                render_markdown_table(
                    ["SR NO.", "Transaction", "Amount"],
                    [[lines[index + 4], lines[index + 5], lines[index + 6]], ["Total", "", lines[index + 8]]],
                )
            )
            output.append("")
            index += 9
            continue

        if emitted_transactions and looks_like_transaction_header_window(lines, index):
            index = skip_statement_transaction_block(lines, index)
            continue

        if emitted_transactions and looks_like_datetime_transaction_header_window(lines, index):
            index = skip_datetime_transaction_block(lines, index)
            continue

        if not emitted_transactions and looks_like_transaction_header_window(lines, index):
            transaction_lines = extract_transaction_sections_from_text("\n".join(lines[index:]))
            if transaction_lines:
                output.extend(transaction_lines)
                output.append("")
                emitted_transactions = True
                index = skip_statement_transaction_block(lines, index)
                continue

        if not emitted_transactions and looks_like_datetime_transaction_header_window(lines, index):
            transaction_lines = extract_datetime_transaction_sections_from_text("\n".join(lines[index:]))
            if transaction_lines:
                output.extend(transaction_lines)
                output.append("")
                emitted_transactions = True
                index = skip_datetime_transaction_block(lines, index)
                continue

        output.append(line)
        index += 1

    return "\n".join(output).strip()


def escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").strip()


def render_markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    table_lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]

    for row in rows:
        table_lines.append(
            "| " + " | ".join(escape_markdown_cell(cell) for cell in row) + " |"
        )

    return table_lines


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def is_separator_row(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(cell != "" and set(cell) <= {"-", ":", " "} for cell in cells)


def context_label(value: str) -> str:
    if re.search(r"\d{2,}X{2,}\d{2,}", value):
        return "Account"
    return "Context"


def normalize_markdown_table_block(lines: list[str]) -> list[str]:
    parsed_rows = [split_markdown_row(line) for line in lines if line.strip("|").strip()]
    if not parsed_rows:
        return []

    logical_rows = [row for row in parsed_rows if not is_separator_row(row)]
    if not logical_rows:
        return []

    width = max(len(row) for row in logical_rows)
    normalized_rows = [row + [""] * (width - len(row)) for row in logical_rows]

    headers = normalized_rows[0]
    body = normalized_rows[1:]
    sections: list[tuple[str | None, list[list[str]]]] = []
    current_context: str | None = None
    current_rows: list[list[str]] = []

    for row in body:
        non_empty = [cell for cell in row if cell]
        if not non_empty:
            continue

        unique_values = {cell for cell in non_empty}
        if len(non_empty) == 1 or len(unique_values) == 1:
            if current_rows:
                sections.append((current_context, current_rows))
                current_rows = []
            context_value = non_empty[0]
            current_context = f"{context_label(context_value)}: {context_value}"
            continue

        current_rows.append(row)

    if current_rows:
        sections.append((current_context, current_rows))

    if not any(headers):
        headers = [f"Column {index + 1}" for index in range(width)]

    if not sections:
        sections = [(None, [])]

    section_lines: list[str] = []
    for section_index, (context, rows_for_section) in enumerate(sections):
        if section_index > 0:
            section_lines.append("")
        if context:
            section_lines.append(context)
            section_lines.append("")
        section_lines.extend(render_markdown_table(headers, rows_for_section))

    return section_lines


def normalize_markdown_tables(markdown: str) -> str:
    normalized_lines: list[str] = []
    table_buffer: list[str] = []

    def flush_table() -> None:
        nonlocal table_buffer
        if not table_buffer:
            return
        normalized_lines.extend(normalize_markdown_table_block(table_buffer))
        table_buffer = []

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            table_buffer.append(line)
            continue

        if stripped and set(stripped) == {"|"}:
            table_buffer.append(line)
            continue

        flush_table()
        normalized_lines.append(line)

    flush_table()
    return "\n".join(normalized_lines)


def parse_generic_transaction_row(line: str) -> list[str] | None:
    match = re.match(
        r"^(\d{2}/\d{2}/\d{4})\|\s*(\d{2}:\d{2})\s+(.*?)\s+Rs\s*([\d,]+\.\d{2})\s+(\S+)$",
        line,
    )
    if not match:
        return None

    tx_date, tx_time, description, amount, indicator = match.groups()
    return [f"{tx_date} {tx_time}", description.strip(), f"Rs {amount}", indicator]


def normalize_table_cell(cell: Any) -> str:
    if cell is None:
        return ""
    return normalize_extracted_text(str(cell)).replace("\n", "<br>")


def normalize_table_rows(rows: list[list[Any]]) -> list[list[str]]:
    cleaned_rows: list[list[str]] = []
    width = max((len(row) for row in rows if row), default=0)

    for row in rows:
        if not row:
            continue
        cleaned = [normalize_table_cell(cell) for cell in row]
        cleaned.extend([""] * (width - len(cleaned)))
        if any(cleaned):
            cleaned_rows.append(cleaned)

    return cleaned_rows


def maybe_expand_single_column_table(rows: list[list[str]]) -> tuple[list[str], list[list[str]], list[str]]:
    if not rows or len(rows[0]) != 1:
        return [], rows, []

    header_text = rows[0][0].lower()
    if not (
        "date" in header_text
        and "description" in header_text
        and "amount" in header_text
    ):
        return [], rows, []

    note_lines: list[str] = []
    parsed_rows: list[list[str]] = []
    body_rows = rows[1:]

    while body_rows and parse_generic_transaction_row(body_rows[0][0]) is None:
        fragments = [fragment.strip() for fragment in body_rows[0][0].split("<br>") if fragment.strip()]
        for fragment in fragments:
            parsed = parse_generic_transaction_row(fragment)
            if parsed is not None:
                parsed_rows.append(parsed)
            else:
                note_lines.append(fragment)
        body_rows = body_rows[1:]

    for row in body_rows:
        parsed = parse_generic_transaction_row(row[0])
        if parsed is None:
            return [], rows, []
        parsed_rows.append(parsed)

    if not parsed_rows:
        return [], rows, []

    return ["Date & Time", "Transaction Description", "Amount", "PI"], parsed_rows, note_lines


def table_to_markdown(table_rows: list[list[Any]], index: int) -> list[str]:
    rows = normalize_table_rows(table_rows)
    if not rows:
        return []

    if len(rows[0]) == 1 and len(rows) < 4:
        return []

    inferred_headers, body_rows, note_lines = maybe_expand_single_column_table(rows)
    if inferred_headers:
        section_lines = [f"### Table {index}", ""]
        section_lines.extend(render_markdown_table(inferred_headers, body_rows))
        if note_lines:
            section_lines.append("")
            section_lines.extend(note_lines)
        section_lines.append("")
        return section_lines

    headers = rows[0]
    body_rows = rows[1:]

    if not any(headers) or len(rows) == 1:
        headers = [f"Column {i + 1}" for i in range(len(rows[0]))]
        body_rows = rows

    section_lines = [f"### Table {index}", ""]
    section_lines.extend(render_markdown_table(headers, body_rows))
    section_lines.append("")
    return section_lines


def extract_page_tables(pdf_path: Path) -> list[list[list[Any]]]:
    tables_by_page: list[list[list[Any]]] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            page_tables = page.extract_tables()
            tables_by_page.append(page_tables)

    return tables_by_page


def extract_selected_page_texts(pdf_path: Path, page_numbers: list[int]) -> dict[int, str]:
    if not page_numbers:
        return {}

    document = pymupdf.open(str(pdf_path))
    page_texts: dict[int, str] = {}

    for page_number in sorted(set(page_numbers)):
        if 1 <= page_number <= document.page_count:
            page_texts[page_number] = normalize_extracted_text(
                document[page_number - 1].get_text("text") or ""
            )

    return page_texts


def has_markdown_table(markdown: str) -> bool:
    return "| ---" in markdown


def page_needs_canonical_transactions(markdown: str) -> bool:
    lowered = markdown.lower()
    return (
        ("transaction" in lowered and "amount" in lowered and ("serno" in lowered or "date" in lowered))
        or bool(re.search(r"\d{4,}X{2,}\d{2,}", markdown))
    )


def build_canonical_page_sections(
    page_text: str,
    page_tables: list[list[Any]] | None,
    base_markdown: str,
    *,
    include_structured_tables: bool = False,
) -> list[str]:
    sections: list[str] = []
    transaction_lines = extract_transaction_sections_from_text(page_text)
    if transaction_lines:
        sections.append(
            "\n".join(transaction_lines).replace("### Transactions", "### Canonical Transactions", 1)
        )

    if include_structured_tables and page_tables and not has_markdown_table(base_markdown):
        structured_tables: list[str] = []
        for table_index, table_rows in enumerate(page_tables, start=1):
            table_lines = table_to_markdown(table_rows, table_index)
            if not table_lines:
                continue
            if table_lines[0].startswith("### Table "):
                table_lines[0] = table_lines[0].replace("### Table ", "### Structured Table ", 1)
            structured_tables.append("\n".join(table_lines))

        sections.extend(structured_tables)

    return [section.strip() for section in sections if section.strip()]


def clean_markdown_output(markdown: str) -> str:
    markdown = re.sub(r"^=== Document parser messages ===.*?(?=\n##|\n#|\Z)", "", markdown, flags=re.S)
    markdown = re.sub(r"\*\*==> picture .*? intentionally omitted <==\*\*\n*", "", markdown)
    markdown = re.sub(r"\*\*----- Start of picture text -----\*\*<br>\n*", "", markdown)
    markdown = re.sub(r"\*\*----- End of picture text -----\*\*<br>\n*", "", markdown)
    markdown = re.sub(r"\*\*Page\s+\d+\s+of\s+\d+\*\*", "", markdown)
    markdown = markdown.replace("©", "'")
    markdown = re.sub(r"(?<![A-Za-z0-9])C\s*(?=\d[\d,]*\.?\d*\b)", "Rs ", markdown)
    markdown = re.sub(r"^- l\s+", "- ", markdown, flags=re.M)
    markdown = re.sub(r"<br>\s*(?=#{1,6}\s)", "\n\n", markdown)
    markdown = re.sub(r"<br>\s*(?=\*\*Page\s+\d+\s+of\s+\d+\*\*)", "\n\n", markdown)
    markdown = normalize_markdown_tables(markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


def convert_with_pymupdf4llm(
    pdf_path: Path,
    page_labels: list[int] | None = None,
    *,
    use_ocr: bool = False,
    force_text: bool = False,
    document_title: str | None = None,
) -> str:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = pymupdf4llm.to_markdown(
            str(pdf_path),
            page_chunks=True,
            use_ocr=use_ocr,
            force_text=force_text,
            show_progress=False,
        )

    if isinstance(result, list):
        chunk_texts = [clean_markdown_output(chunk.get("text", "")) for chunk in result]
        candidate_pages = [
            page_index
            for page_index, text in enumerate(chunk_texts, start=1)
            if page_needs_canonical_transactions(text)
        ]
        page_texts = extract_selected_page_texts(pdf_path, candidate_pages)
        page_sections: list[str] = []
        for page_index, text in enumerate(chunk_texts, start=1):
            page_label = page_labels[page_index - 1] if page_labels and page_index - 1 < len(page_labels) else page_index
            page_text = page_texts.get(page_index, "")
            if page_text:
                canonical_sections = build_canonical_page_sections(page_text, None, text)
                text = "\n\n".join(part for part in [text, *canonical_sections] if part)
            if not text:
                continue
            page_sections.append(f"## Page {page_label}\n\n{text}")

        if page_sections:
            title = document_title or markdown_title_from_filename(pdf_path.stem.removesuffix("_unlocked"))
            return f"# {title}\n\n" + "\n\n---\n\n".join(page_sections)

    return clean_markdown_output(str(result))


def extract_text_markdown(
    pdf_path: Path,
    page_labels: list[int] | None = None,
    *,
    include_tables: bool = True,
    include_selective_ocr: bool = True,
    document_title: str | None = None,
) -> tuple[str, int]:
    document = pymupdf.open(str(pdf_path))
    tables_by_page = extract_page_tables(pdf_path) if include_tables else []
    page_sections: list[str] = []
    total_chars = 0

    for page_index in range(1, document.page_count + 1):
        page_label = page_labels[page_index - 1] if page_labels and page_index - 1 < len(page_labels) else page_index
        page = document[page_index - 1]
        text = extract_sorted_page_text(
            page,
            include_selective_ocr=include_selective_ocr,
        )
        total_chars += len(text)
        page_lines: list[str] = [f"## Page {page_label}", ""]

        if text:
            page_lines.append(format_generic_text_page(text))
            page_lines.append("")

        page_tables = tables_by_page[page_index - 1] if page_index - 1 < len(tables_by_page) else []
        for table_index, table_rows in enumerate(page_tables, start=1):
            table_lines = table_to_markdown(table_rows, table_index)
            if table_lines:
                page_lines.extend(table_lines)

        page_sections.append("\n".join(page_lines).strip())

    if not page_sections:
        return "", 0

    title = document_title or markdown_title_from_filename(pdf_path.stem.removesuffix("_unlocked"))
    markdown = f"# {title}\n\n" + "\n\n---\n\n".join(page_sections)
    return markdown, total_chars


def is_text_based_pdf(pdf_path: Path, sample_pages: int = 3) -> bool:
    document = pymupdf.open(str(pdf_path))
    chars = 0
    checked = 0

    for page_index in range(min(sample_pages, document.page_count)):
        text = normalize_extracted_text(document[page_index].get_text("text") or "")
        chars += len(text)
        checked += 1

    if checked == 0:
        return False

    return chars >= 500


def convert_with_docling(source_path: Path) -> str:
    result = get_converter().convert(str(source_path))
    return result.document.export_to_markdown().strip()


def convert_with_docling_per_page(
    pdf_path: Path,
    page_labels: list[int] | None = None,
    *,
    document_title: str | None = None,
) -> str:
    reader = PdfReader(str(pdf_path))
    page_sections: list[str] = []

    with tempfile.TemporaryDirectory(dir=str(TMP_DIR)) as temp_dir:
        temp_root = Path(temp_dir)

        for page_index, page in enumerate(reader.pages, start=1):
            page_label = page_labels[page_index - 1] if page_labels and page_index - 1 < len(page_labels) else page_index
            page_path = temp_root / f"page_{page_label}.pdf"
            writer = PdfWriter()
            writer.add_page(page)
            with page_path.open("wb") as handle:
                writer.write(handle)

            page_markdown = clean_markdown_output(convert_with_docling(page_path))
            if not page_markdown:
                continue
            page_sections.append(f"## Page {page_label}\n\n{page_markdown.strip()}")

    if not page_sections:
        return ""

    title = document_title or markdown_title_from_filename(pdf_path.stem.removesuffix("_unlocked"))
    return f"# {title}\n\n" + "\n\n---\n\n".join(page_sections)


def convert_pdf_to_markdown(
    pdf_path: Path,
    page_labels: list[int] | None = None,
    *,
    document_title: str | None = None,
    ocr_mode: str = "off",
) -> tuple[str, Path]:
    markdown = ""
    text_based = False
    use_selective_ocr = ocr_mode_uses_selective_ocr(ocr_mode)
    total_chars = 0

    try:
        markdown = clean_markdown_output(convert_with_docling(pdf_path))
    except Exception:
        markdown = ""

    try:
        text_based = is_text_based_pdf(pdf_path)
    except Exception:
        text_based = False

    if text_based:
        if use_selective_ocr:
            try:
                markdown = convert_with_docling_per_page(
                    pdf_path,
                    page_labels=page_labels,
                    document_title=document_title,
                )
            except Exception:
                pass

        if len(markdown.strip()) < 200:
            markdown, total_chars = extract_text_markdown(
                pdf_path,
                page_labels=page_labels,
                include_tables=False,
                include_selective_ocr=False,
                document_title=document_title,
            )
            if total_chars < 400:
                try:
                    markdown = convert_with_pymupdf4llm(
                        pdf_path,
                        page_labels=page_labels,
                        use_ocr=False,
                        force_text=False,
                        document_title=document_title,
                    )
                except Exception:
                    markdown = clean_markdown_output(convert_with_docling(pdf_path))
    else:
        try:
            markdown = convert_with_pymupdf4llm(
                pdf_path,
                page_labels=page_labels,
                use_ocr=True,
                force_text=True,
                document_title=document_title,
            )
        except Exception:
            markdown = ""

        if len(markdown.strip()) < 200:
            markdown, total_chars = extract_text_markdown(
                pdf_path,
                page_labels=page_labels,
                include_tables=True,
                include_selective_ocr=True,
                document_title=document_title,
            )
            if total_chars < 400:
                markdown = clean_markdown_output(convert_with_docling(pdf_path))

    if use_selective_ocr and markdown.strip():
        markdown = append_image_ocr_sections(markdown, pdf_path, page_labels)

    if len(markdown.strip()) >= 200 and not use_selective_ocr:
        output_name = f"{pdf_path.stem}.md"
        output_path = OUTPUTS_DIR / output_name
        output_path.write_text(markdown, encoding="utf-8")
        return markdown, output_path

    output_name = f"{pdf_path.stem}.md"
    output_path = OUTPUTS_DIR / output_name
    output_path.write_text(markdown, encoding="utf-8")
    return markdown, output_path


def convert_document_to_markdown(
    source_path: Path,
    *,
    password: str = "",
    pages_value: str = "",
    document_title: str | None = None,
    ocr_mode: str = "off",
) -> tuple[str, Path, list[int], int]:
    if is_pdf_path(source_path):
        ready_pdf = unlock_pdf_if_needed(source_path, password)
        parse_pdf, selected_pages, total_pages = select_pdf_pages(ready_pdf, pages_value)
        markdown, output_path = convert_pdf_to_markdown(
            parse_pdf,
            page_labels=selected_pages,
            document_title=document_title,
            ocr_mode=ocr_mode,
        )
        return markdown, output_path, selected_pages, total_pages

    markdown = clean_markdown_output(convert_with_docling(source_path))
    output_name = f"{source_path.stem}.md"
    output_path = OUTPUTS_DIR / output_name
    output_path.write_text(markdown, encoding="utf-8")
    return markdown, output_path, [], 0


def write_warmup_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "Docling startup warmup")
    page.insert_text((72, 104), "ICICI BANK")
    document.save(path)
    document.close()


def warmup_runtime() -> None:
    started_at = time.time()
    configure_temp_directory()
    print("Warming Docling runtime...")
    get_ocr_engine()
    get_converter()

    warmup_dir = TMP_DIR / "warmup"
    warmup_dir.mkdir(parents=True, exist_ok=True)
    warmup_pdf = warmup_dir / "startup-warmup.pdf"
    write_warmup_pdf(warmup_pdf)
    convert_pdf_to_markdown(
        warmup_pdf,
        document_title="Startup Warmup",
        ocr_mode="selective",
    )
    duration = time.time() - started_at
    print(f"Docling runtime warmup complete in {duration:.2f}s")


def warmup_runtime_background() -> None:
    try:
        warmup_runtime()
    except Exception as exc:
        print(f"Docling runtime warmup failed: {exc}")


class PdfMarkdownHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.respond_html(render_page())
            return

        if parsed.path == "/healthz":
            self.respond_text("ok\n")
            return

        if parsed.path == "/download":
            self.handle_download(parsed.query)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/convert":
            self.handle_api_convert()
            return

        if parsed.path != "/convert":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        try:
            content_type = self.headers.get("Content-Type", "")
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            fields, files = parse_multipart_form(content_type, body)

            upload = next((files.get(field_name) for field_name in UPLOAD_FIELD_NAMES if files.get(field_name)), None)
            if upload is None:
                raise AppError("Choose a supported document file to upload.")

            filename, payload = upload
            password = fields.get("password", "").strip()
            pages_value = fields.get("pages", "").strip()
            ocr_mode = normalize_ocr_mode(fields.get("ocr_mode", "off"))
            saved_document = save_upload_bytes(payload, filename)
            markdown, output_path, _, _ = convert_document_to_markdown(
                saved_document,
                password=password,
                pages_value=pages_value,
                document_title=markdown_title_from_filename(filename),
                ocr_mode=ocr_mode,
            )
            self.respond_html(
                render_page(
                    markdown=markdown,
                    output_name=output_path.name,
                    pages_value=pages_value,
                    ocr_mode=ocr_mode,
                )
            )
        except AppError as exc:
            self.respond_html(
                render_page(
                    error=str(exc),
                    pages_value=fields.get("pages", "").strip() if "fields" in locals() else "",
                    ocr_mode=fields.get("ocr_mode", "off").strip() if "fields" in locals() else "off",
                ),
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:  # pragma: no cover - local server guardrail
            self.respond_html(
                render_page(
                    error=f"Unexpected error: {exc}",
                    pages_value=fields.get("pages", "").strip() if "fields" in locals() else "",
                    ocr_mode=fields.get("ocr_mode", "off").strip() if "fields" in locals() else "off",
                ),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def handle_api_convert(self) -> None:
        response_format = self.api_response_format()
        try:
            saved_document, password, original_filename, pages_value, ocr_mode = self.parse_api_input()
            markdown, output_path, selected_pages, total_pages = convert_document_to_markdown(
                saved_document,
                password=password,
                pages_value=pages_value,
                document_title=markdown_title_from_filename(original_filename),
                ocr_mode=ocr_mode,
            )
            if response_format == "markdown":
                self.respond_markdown(markdown)
                return
            self.respond_json(
                {
                    "success": True,
                    "filename": original_filename,
                    "stored_filename": saved_document.name,
                    "output_filename": output_path.name,
                    "page_count": len(selected_pages) if selected_pages else None,
                    "total_page_count": total_pages or None,
                    "selected_pages": selected_pages,
                    "ocr_mode": ocr_mode,
                    "is_pdf": is_pdf_path(saved_document),
                    "markdown": markdown,
                }
            )
        except AppError as exc:
            if response_format == "markdown":
                self.respond_text(str(exc) + "\n", status=HTTPStatus.BAD_REQUEST)
                return
            self.respond_json(
                {"success": False, "error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:  # pragma: no cover - local server guardrail
            if response_format == "markdown":
                self.respond_text(
                    f"Unexpected error: {exc}\n",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self.respond_json(
                {"success": False, "error": f"Unexpected error: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def api_response_format(self) -> str:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        requested_format = query.get("format", [""])[0].strip().lower()
        if requested_format == "markdown":
            return "markdown"

        accept = self.headers.get("Accept", "").lower()
        if "text/markdown" in accept or "text/plain" in accept:
            return "markdown"

        return "json"

    def parse_api_input(self) -> tuple[Path, str, str, str, str]:
        content_type = self.headers.get("Content-Type", "")

        if content_type.startswith("application/json"):
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise AppError(f"Invalid JSON body: {exc.msg}") from exc

            file_base64 = payload.get("document_base64") or payload.get("file_base64") or payload.get("pdf_base64")
            if not file_base64:
                raise AppError("JSON body must include `document_base64`, `file_base64`, or `pdf_base64`.")

            filename = str(payload.get("filename", "upload.pdf") or "upload.pdf")
            password = str(payload.get("password", "") or "").strip()
            pages_value = str(payload.get("pages", "") or "").strip()
            ocr_mode = normalize_ocr_mode(str(payload.get("ocr_mode", "off") or "off"))
            try:
                file_bytes = base64.b64decode(file_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise AppError("The provided base64 document payload is invalid.") from exc

            return save_upload_bytes(file_bytes, filename), password, filename, pages_value, ocr_mode

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        fields, files = parse_multipart_form(content_type, raw_body)

        upload = next((files.get(field_name) for field_name in UPLOAD_FIELD_NAMES if files.get(field_name)), None)
        if upload is None:
            raise AppError("Multipart form must include a `document`, `file`, or `pdf` field.")

        original_filename, file_bytes = upload
        password = fields.get("password", "").strip()
        pages_value = fields.get("pages", "").strip()
        ocr_mode = normalize_ocr_mode(fields.get("ocr_mode", "off"))
        return save_upload_bytes(file_bytes, original_filename), password, original_filename, pages_value, ocr_mode

    def handle_download(self, query_string: str) -> None:
        params = parse_qs(query_string)
        name = params.get("name", [""])[0]
        if not name:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing file name")
            return

        requested = OUTPUTS_DIR / Path(name).name
        if not requested.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        payload = requested.read_bytes()
        content_type = mimetypes.guess_type(requested.name)[0] or "text/markdown"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{requested.name}"',
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def respond_html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def respond_json(
        self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_markdown(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    configure_temp_directory()
    server = ThreadingHTTPServer((HOST, PORT), PdfMarkdownHandler)
    print(f"Serving PDF to Markdown app on http://{HOST}:{PORT}")
    if PREWARM_ON_STARTUP:
        threading.Thread(target=warmup_runtime_background, name="docling-warmup", daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
