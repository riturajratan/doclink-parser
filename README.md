# Docling Document Reader

Small local web app that uploads a supported document, runs it through Docling, and returns Markdown in the browser or API.

## Run

```bash
cd /Users/riturajratan/projects/docling-pdf-markdown
python3 app.py
```

Open `http://127.0.0.1:8010`.

## Optional isolated environment

```bash
cd /Users/riturajratan/projects/docling-pdf-markdown
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

## Notes

- Uploaded files are stored under `storage/uploads/`.
- Generated Markdown files are stored under `storage/outputs/`.
- PDF, DOCX, PPTX, XLSX, Markdown, HTML, CSV, VTT, and common image formats are supported.
- If the file is an encrypted PDF, enter the document password in the form before converting.
- You can limit parsing to specific pages with values like `1-3` or `2,5,7` for PDFs.
- For PDFs, set `ocr_mode=selective` when you want text pulled from embedded images such as bank logos, headers, or scanned snippets.
- Default port is `8010`. Override it with `PORT=9000 python3 app.py` if needed.

## Smoke test

```bash
cd /Users/riturajratan/projects/docling-pdf-markdown
python3 check_docling.py
```

This creates temporary Markdown, HTML, and PDF samples and verifies that Docling can read them.

## API

### `POST /api/convert`

Returns JSON with the extracted Markdown.

Multipart form-data fields:

- `document`: preferred upload field for the source file
- `file`: alternate upload field
- `pdf`: backward-compatible upload field for PDFs
- `password`: optional PDF password
- `pages`: optional page selection like `1-3` or `2,5,7` for PDFs

Example:

```bash
curl -X POST http://127.0.0.1:8010/api/convert \
  -F "document=@/path/to/file.pdf" \
  -F "password=secret" \
  -F "pages=1-3"
```

JSON body is also supported:

```json
{
  "filename": "file.pdf",
  "password": "secret",
  "pages": "1-3",
  "document_base64": "BASE64_ENCODED_FILE"
}
```

## Railway

This app is ready for Railway-style deployment because it reads `PORT` from the environment and listens on `0.0.0.0`.

The repo now includes a root [Dockerfile](/Users/riturajratan/Projects/docling-pdf-markdown/Dockerfile:1), and Railway will prefer that automatically. This avoids the missing `libxcb.so.1` / OpenCV runtime issue by explicitly installing the required native OCR and image-processing libraries. On Debian `trixie`, this uses `libglx-mesa0` instead of the removed transitional package `libgl1-mesa-glx`.

The image also runs [warmup_models.py](/Users/riturajratan/Projects/docling-pdf-markdown/warmup_models.py:1) during build so RapidOCR and Docling models are downloaded before the app handles live traffic. The server repeats a lightweight warmup on startup, which helps avoid first-request timeouts on platforms like Railway.

Suggested start command:

```bash
python3 app.py
```

Suggested health check path:

```text
/healthz
```
