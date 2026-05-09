# PDF to Markdown

Small local web app that uploads a PDF, optionally unlocks it with a password, converts it into Markdown, and returns the result in the browser or API.

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

- Uploaded PDFs are stored under `storage/uploads/`.
- Generated Markdown files are stored under `storage/outputs/`.
- If the PDF is encrypted, enter the document password in the form before converting.
- You can limit parsing to specific pages with values like `1-3` or `2,5,7`.
- Default port is `8010`. Override it with `PORT=9000 python3 app.py` if needed.

## API

### `POST /api/convert`

Returns JSON with the extracted Markdown.

Multipart form-data fields:

- `pdf`: PDF file
- `password`: optional PDF password
- `pages`: optional page selection like `1-3` or `2,5,7`

Example:

```bash
curl -X POST http://127.0.0.1:8010/api/convert \
  -F "pdf=@/path/to/file.pdf" \
  -F "password=secret" \
  -F "pages=1-3"
```

JSON body is also supported:

```json
{
  "filename": "file.pdf",
  "password": "secret",
  "pages": "1-3",
  "pdf_base64": "BASE64_ENCODED_PDF"
}
```

## Railway

This app is ready for Railway-style deployment because it reads `PORT` from the environment and listens on `0.0.0.0`.

Suggested start command:

```bash
python3 app.py
```

Suggested health check path:

```text
/healthz
```
