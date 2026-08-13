# PDF Generator API

FastAPI service that fills a selected HTML template with tags and returns a
generated PDF, rendered via headless Chromium (Playwright).

## Project layout

```
app/
  main.py             FastAPI app: /templates, /templates/{id}/tags, /generate
  templates_service.py  Template discovery, id resolution, placeholder detection
  pdf_service.py      Shared headless-Chromium HTML -> PDF renderer
templates/
  invoice.html         Sample template ({{ invoice_number }}, {{ customer_name }}, ...)
  certificate.html     Sample template ({{ recipient_name }}, {{ course_name }}, ...)
```

Templates are plain HTML files using Jinja2 `{{ tag }}` placeholders. A
template's id is its filename without the `.html` extension.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Run

```powershell
python -m uvicorn app.main:app
```

The API is served at <http://127.0.0.1:8000>, with interactive docs at
<http://127.0.0.1:8000/docs>.

> **Do not use `--reload` (or `--workers` > 1) on Windows.** Uvicorn's
> auto-reload and multi-worker modes force the `SelectorEventLoop`, which
> does not support subprocesses on Windows — Playwright's Chromium launch
> will fail at startup with `NotImplementedError`. Restart the server
> manually after code changes instead.

## API

### `GET /templates`

List available templates with their required tags and tag schema.

### `POST /templates`

Create a new template, or replace an existing one, from raw HTML.

Request body:

```json
{
  "template_id": "receipt",
  "html": "<html><body><h1>Receipt {{ receipt_number }}</h1></body></html>",
  "overwrite": false
}
```

- `template_id` — letters, numbers, underscores and hyphens only (this also
  rules out path traversal). Becomes the filename `templates/<template_id>.html`.
- `html` — the full template source, using Jinja2 `{{ tag }}` placeholders.
  Must be non-empty, syntactically valid Jinja2, and under 512 KB.
- `overwrite` — defaults to `false`; if a template with the same id already
  exists and `overwrite` isn't `true`, the request is rejected.

Responds with the new template's metadata (same shape as `GET /templates`
entries): `201 Created` for a brand-new template, `200 OK` when an existing
one was overwritten.

Errors: `400 Bad Request` for an invalid id, empty/oversized content, or
invalid Jinja2 syntax; `409 Conflict` if the id already exists and
`overwrite` is `false`.

### `GET /templates/{template_id}/tags`

List the tags required by a specific template, e.g. for `invoice`:

```json
{
  "template_id": "invoice",
  "tags": [
    "amount",
    "customer_name",
    "invoice_date",
    "invoice_lines",
    "invoice_number"
  ],
  "tag_schema": {
    "invoice_number": null,
    "invoice_date": null,
    "customer_name": null,
    "invoice_lines": {
      "description": null,
      "quantity": null,
      "unit_price": null,
      "line_total": null,
      "invoice_details": {
        "sku": null,
        "warehouse": null
      }
    },
    "amount": null
  }
}
```

`tags` is the flat list of top-level tag names. `tag_schema` maps each tag
to `null` for a plain scalar value, or to a nested dict describing the
fields required per entry when the tag is iterated as an array (e.g.
`invoice_lines`, looped over with `{% for line in invoice_lines %}`). Those
fields can themselves be arrays — `invoice_details` above is itself a list
per line item, looped over with `{% for detail in line.invoice_details %}`
and accessed as `detail.sku` — arrays can be nested inside arrays to any
depth.

### `POST /generate`

Render a template with tags and return the PDF.

Request body:

```json
{
  "template_id": "invoice",
  "tags": {
    "invoice_number": "INV-1001",
    "invoice_date": "2026-08-13",
    "customer_name": "Acme Corp",
    "invoice_lines": [
      {
        "description": "Consulting services",
        "quantity": 10,
        "unit_price": "$100.00",
        "line_total": "$1,000.00",
        "invoice_details": [
          { "sku": "CNS-001", "warehouse": "WH-North" },
          { "sku": "CNS-002", "warehouse": "WH-East" }
        ]
      },
      {
        "description": "Support retainer",
        "quantity": 1,
        "unit_price": "$250.00",
        "line_total": "$250.00",
        "invoice_details": [{ "sku": "SUP-002", "warehouse": "WH-South" }]
      }
    ],
    "amount": "$1,250.00"
  }
}
```

Tag values may be strings, numbers, booleans, or arrays/objects, nested to
any depth — the `invoice` template above loops over `invoice_lines` with
Jinja2's `{% for line in invoice_lines %}` to render one row per line item,
and each line item loops over its own `invoice_details` array (a list, not
just a single object) with `{% for detail in line.invoice_details %}` and
`detail.sku`.

Response: `application/pdf` binary stream (downloadable file).

Missing required tags return `400 Bad Request`; an unknown `template_id`
returns `404 Not Found`.

## Adding a new template

Either:

- Call `POST /templates` (see above) with your template's markup in the
  `html` field — no file system or server access needed, or
- Add a new `.html` file directly under `templates/`, using `{{ tag_name }}`
  for any dynamic value; it's picked up automatically.

Either way, call `GET /templates` to confirm the new template's id and
required tags.

## Notes

- Template ids are resolved against an allowlist of existing files in
  `templates/`; path separators and `..` are rejected to prevent path
  traversal.
- Tag values are HTML-escaped by Jinja2's autoescaping, so they cannot inject
  markup/script into the generated document.
- A single Chromium instance is started at app startup and reused across
  requests for performance; it is closed on shutdown.
