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
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pdfplumber
import pymupdf4llm
from pypdf import PdfReader, PdfWriter


HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8010"))
PROJECT_ROOT = Path(__file__).resolve().parent
STORAGE_DIR = PROJECT_ROOT / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
OUTPUTS_DIR = STORAGE_DIR / "outputs"
TMP_DIR = STORAGE_DIR / "tmp"
CONVERTER: Any | None = None


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
        configure_temp_directory()
        from docling.document_converter import DocumentConverter

        CONVERTER = DocumentConverter()

    return CONVERTER


def sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    return cleaned or "upload.pdf"


def render_page(*, markdown: str = "", error: str = "", output_name: str = "") -> bytes:
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
  <title>PDF to Markdown</title>
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
    input[type="password"] {{
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
        <p class="loading-title">Processing PDF</p>
        <p class="loading-copy">Decrypting, extracting structure, and preparing markdown. Large or scanned PDFs may take longer.</p>
      </div>
    </div>
    <section class="hero">
      <h1>PDF to Markdown</h1>
      <p>Upload a PDF, optionally provide a password for encrypted files, and get Markdown generated through Docling.</p>
    </section>

    <section class="card">
      <form id="convertForm" action="/convert" method="post" enctype="multipart/form-data">
        <div>
          <label for="pdf">PDF file</label>
          <input id="pdf" name="pdf" type="file" accept="application/pdf,.pdf" required>
          <p class="hint">Regular PDFs work directly. Encrypted PDFs need the document password below.</p>
        </div>

        <div>
          <label for="password">PDF password (optional)</label>
          <input id="password" name="password" type="password" placeholder="Only needed for locked PDFs">
        </div>

        <button id="submitButton" type="submit">Convert to Markdown</button>
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


def save_pdf_bytes(payload: bytes, filename: str) -> Path:
    original_name = sanitize_filename(Path(filename or "upload.pdf").name)
    if not original_name.lower().endswith(".pdf"):
        raise AppError("Only PDF files are supported.")
    if not payload:
        raise AppError("The uploaded file is empty.")
    upload_name = f"{uuid4().hex}_{original_name}"
    upload_path = UPLOADS_DIR / upload_name
    upload_path.write_bytes(payload)
    return upload_path


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
    text = re.sub(r"(?<![A-Za-z0-9])C\s*(?=\d[\d,]*\.\d{2}\b)", "Rs ", text)
    lines: list[str] = []
    previous_blank = False

    for raw_line in text.splitlines():
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


def convert_with_pymupdf4llm(pdf_path: Path) -> str:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)

    if isinstance(result, list):
        page_sections: list[str] = []
        for page_index, chunk in enumerate(result, start=1):
            text = clean_markdown_output(chunk.get("text", ""))
            if not text:
                continue
            page_sections.append(f"## Page {page_index}\n\n{text}")

        if page_sections:
            title = pdf_path.stem.removesuffix("_unlocked").replace("_", " ")
            return f"# {title}\n\n" + "\n\n---\n\n".join(page_sections)

    return clean_markdown_output(str(result))


def extract_text_markdown(pdf_path: Path) -> tuple[str, int]:
    reader = PdfReader(str(pdf_path))
    tables_by_page = extract_page_tables(pdf_path)
    page_sections: list[str] = []
    total_chars = 0

    for page_index, page in enumerate(reader.pages, start=1):
        text = normalize_extracted_text(page.extract_text() or "")
        total_chars += len(text)
        page_lines: list[str] = [f"## Page {page_index}", ""]

        if text:
            page_lines.append(text)
            page_lines.append("")

        page_tables = tables_by_page[page_index - 1] if page_index - 1 < len(tables_by_page) else []
        for table_index, table_rows in enumerate(page_tables, start=1):
            table_lines = table_to_markdown(table_rows, table_index)
            if table_lines:
                page_lines.extend(table_lines)

        page_sections.append("\n".join(page_lines).strip())

    if not page_sections:
        return "", 0

    title_stem = pdf_path.stem.removesuffix("_unlocked")
    title = title_stem.replace("_", " ")
    markdown = f"# {title}\n\n" + "\n\n---\n\n".join(page_sections)
    return markdown, total_chars


def convert_pdf_to_markdown(pdf_path: Path) -> tuple[str, Path]:
    markdown = ""

    try:
        markdown = convert_with_pymupdf4llm(pdf_path)
    except Exception:
        markdown = ""

    if len(markdown.strip()) < 200:
        markdown, total_chars = extract_text_markdown(pdf_path)

        # Final fallback for image-heavy / scanned PDFs.
        if total_chars < 400:
            result = get_converter().convert(str(pdf_path))
            markdown = result.document.export_to_markdown()

    output_name = f"{pdf_path.stem}.md"
    output_path = OUTPUTS_DIR / output_name
    output_path.write_text(markdown, encoding="utf-8")
    return markdown, output_path


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
        if self.path == "/api/convert":
            self.handle_api_convert()
            return

        if self.path != "/convert":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        try:
            content_type = self.headers.get("Content-Type", "")
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            fields, files = parse_multipart_form(content_type, body)

            pdf_file = files.get("pdf")
            if pdf_file is None:
                raise AppError("Choose a PDF file to upload.")

            filename, payload = pdf_file
            password = fields.get("password", "").strip()
            saved_pdf = save_pdf_bytes(payload, filename)
            ready_pdf = unlock_pdf_if_needed(saved_pdf, password)
            markdown, output_path = convert_pdf_to_markdown(ready_pdf)
            self.respond_html(
                render_page(markdown=markdown, output_name=output_path.name)
            )
        except AppError as exc:
            self.respond_html(render_page(error=str(exc)), status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - local server guardrail
            self.respond_html(
                render_page(error=f"Unexpected error: {exc}"),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def handle_api_convert(self) -> None:
        try:
            saved_pdf, password, original_filename = self.parse_api_input()
            ready_pdf = unlock_pdf_if_needed(saved_pdf, password)
            markdown, output_path = convert_pdf_to_markdown(ready_pdf)
            page_count = len(PdfReader(str(ready_pdf)).pages)
            self.respond_json(
                {
                    "success": True,
                    "filename": original_filename,
                    "stored_filename": ready_pdf.name,
                    "output_filename": output_path.name,
                    "page_count": page_count,
                    "markdown": markdown,
                }
            )
        except AppError as exc:
            self.respond_json(
                {"success": False, "error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:  # pragma: no cover - local server guardrail
            self.respond_json(
                {"success": False, "error": f"Unexpected error: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def parse_api_input(self) -> tuple[Path, str, str]:
        content_type = self.headers.get("Content-Type", "")

        if content_type.startswith("application/json"):
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise AppError(f"Invalid JSON body: {exc.msg}") from exc

            pdf_base64 = payload.get("pdf_base64") or payload.get("file_base64")
            if not pdf_base64:
                raise AppError("JSON body must include `pdf_base64`.")

            filename = str(payload.get("filename", "upload.pdf") or "upload.pdf")
            password = str(payload.get("password", "") or "").strip()
            try:
                pdf_bytes = base64.b64decode(pdf_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise AppError("`pdf_base64` is not valid base64.") from exc

            return save_pdf_bytes(pdf_bytes, filename), password, filename

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        fields, files = parse_multipart_form(content_type, raw_body)

        pdf_file = files.get("pdf")
        if pdf_file is None:
            raise AppError("Multipart form must include a `pdf` file field.")

        original_filename, pdf_bytes = pdf_file
        password = fields.get("password", "").strip()
        return save_pdf_bytes(pdf_bytes, original_filename), password, original_filename

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

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    configure_temp_directory()
    server = ThreadingHTTPServer((HOST, PORT), PdfMarkdownHandler)
    print(f"Serving PDF to Markdown app on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
