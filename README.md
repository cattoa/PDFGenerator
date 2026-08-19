# PDF Generator API

FastAPI service that fills a selected HTML template with tags and returns a
generated PDF, rendered via headless Chromium (Playwright).

## Project layout

```
app/
  main.py             FastAPI app: /templates, /templates/{id}/tags, /templates/{id}/migrate, /generate
  templates_service.py  Template discovery, id resolution, placeholder detection, migration
  pdf_service.py      Shared headless-Chromium HTML -> PDF renderer
  config.py           Settings (templates dirs, output dir, host/port) via env vars
  cli.py              `pdfgenerator` console-script entry point
templates/              Non-production template store (Development, Test, UAT, ...)
  invoice.html         Sample template ({{ invoice_number }}, {{ customer_name }}, ...)
  certificate.html     Sample template ({{ recipient_name }}, {{ course_name }}, ...)
  production/           Production template store
    invoice.html         The promoted, production copy of a template
```

Templates are plain HTML files using Jinja2 `{{ tag }}` placeholders. A
template's id is its filename without the `.html` extension.

## Environments

Every request carries a mandatory `environment` tag naming the deployment
environment it targets, and that tag selects which template store the request
reads and writes:

| `environment`                          | Template store                                 |
| -------------------------------------- | ---------------------------------------------- |
| `Production` (matched case-insensitively) | `templates/production/`                      |
| anything else (`Development`, `Test`, `UAT`, ...) | `templates/` (shared by all non-prod envs) |

The tag is a JSON body field on `POST` requests (`/templates`, `/generate`,
`/templates/{id}/migrate`) and a **query parameter** on the requests that have
no body (`GET /templates`, `GET /templates/{id}/tags`,
`DELETE /templates/{id}`). It is always required — omitting it, or sending an
empty string, returns `422 Unprocessable Entity`.

The two stores are fully independent: the same template id can hold different
markup per environment, `GET /templates` only ever lists the store for the
environment you asked for (the nested `production/` directory is never
reported as part of the non-production store), and `DELETE` only removes the
copy in the environment you named. Move templates between the two with
[`POST /templates/{template_id}/migrate`](#post-templatestemplate_idmigrate).

Note that `environment` is routing metadata, not a template placeholder — it
is not injected into the rendered HTML. A template that wants to print its
environment should declare its own `{{ ... }}` tag and be passed it in `tags`.

## Configuration

Settings are read from `PDFGEN_*` environment variables (or a `.env` file in
the working directory):

| Variable                          | Default                          | Purpose                                                     |
| --------------------------------- | -------------------------------- | ----------------------------------------------------------- |
| `PDFGEN_TEMPLATES_DIR`            | `<project>/templates`            | Non-production `.html` template store                       |
| `PDFGEN_PRODUCTION_TEMPLATES_DIR` | `<templates dir>/production`     | Production `.html` template store                           |
| `PDFGEN_OUTPUT_DIR`               | `<project>/output`               | Directory hardcopies are written to (see `/generate`)       |
| `PDFGEN_HOST`                     | `127.0.0.1`                      | Bind host used by the `pdfgenerator` CLI                    |
| `PDFGEN_PORT`                     | `8000`                           | Bind port used by the `pdfgenerator` CLI                    |

`PDFGEN_PRODUCTION_TEMPLATES_DIR` defaults to a `production/` sub-directory of
`PDFGEN_TEMPLATES_DIR`, so setting the templates dir alone is enough; set it
explicitly only if the production store must live somewhere else entirely.
Both directories are created at startup if missing.

When installed as a package (see below), `PDFGEN_TEMPLATES_DIR` and
`PDFGEN_OUTPUT_DIR` should normally be set explicitly, since the packaged
install has no project-root `templates/`/`output/` folder alongside it.

## Install as a package

```powershell
python -m pip install .
```

This installs the `pdfgenerator` console script:

```powershell
$env:PDFGEN_TEMPLATES_DIR = "C:\path\to\templates"
$env:PDFGEN_OUTPUT_DIR = "C:\path\to\output"
pdfgenerator --host 0.0.0.0 --port 8000
```

For local development, install in editable mode instead (`pip install -e .`).

## Deploying to a server

Build a wheel and ship just that, rather than copying the working tree:

```powershell
python -m pip install build
python -m build
```

Copy `dist/pdfgenerator-1.1.0-py3-none-any.whl` to the server, along with your
`templates/` directory — including its `production/` sub-directory — (create an
`output/` directory too) — these live outside the package and are located via
`PDFGEN_TEMPLATES_DIR` / `PDFGEN_PRODUCTION_TEMPLATES_DIR` /
`PDFGEN_OUTPUT_DIR`. On the server:

```bash
python -m venv .venv
.venv/bin/pip install pdfgenerator-1.1.0-py3-none-any.whl
.venv/bin/playwright install chromium --with-deps
export PDFGEN_TEMPLATES_DIR=/opt/pdfgenerator/templates
export PDFGEN_OUTPUT_DIR=/opt/pdfgenerator/output
export PDFGEN_HOST=0.0.0.0
.venv/bin/python -m app.cli
```

Alternatively, copy the source tree (`app/`, `templates/`, `requirements.txt`,
`pyproject.toml` — skip `.venv/`, `output/`, `__pycache__/`) and install with
`pip install -r requirements.txt` on the server instead of using a wheel.

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

All endpoints below require the `environment` tag — see
[Environments](#environments).

### `GET /templates`

List the templates available in an environment, with their required tags and
tag schema.

```
GET /templates?environment=Development
```

Each entry echoes the `environment` it was read from.

### `POST /templates`

Create a new template, or replace an existing one, from raw HTML, in the store
backing `environment`.

Request body:

```json
{
  "template_id": "receipt",
  "environment": "Development",
  "html": "<html><body><h1>Receipt {{ receipt_number }}</h1></body></html>",
  "overwrite": false
}
```

- `template_id` — letters, numbers, underscores and hyphens only (this also
  rules out path traversal). Becomes the filename `<store>/<template_id>.html`.
- `environment` — mandatory; `Production` writes to `templates/production/`,
  any other value writes to `templates/`.
- `html` — the full template source, using Jinja2 `{{ tag }}` placeholders.
  Must be non-empty, syntactically valid Jinja2, and under 512 KB.
- `overwrite` — defaults to `false`; if a template with the same id already
  exists **in that environment** and `overwrite` isn't `true`, the request is
  rejected. An id already used in the *other* environment is not a conflict.

Responds with the new template's metadata (same shape as `GET /templates`
entries): `201 Created` for a brand-new template, `200 OK` when an existing
one was overwritten.

Errors: `400 Bad Request` for an invalid id, empty/oversized content, or
invalid Jinja2 syntax; `409 Conflict` if the id already exists and
`overwrite` is `false`.

### `GET /templates/{template_id}/tags`

List the tags required by a specific template in an environment, e.g. for
`invoice`:

```
GET /templates/invoice/tags?environment=Development
```

```json
{
  "template_id": "invoice",
  "environment": "Development",
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

### `POST /templates/{template_id}/migrate`

Copy a template between the non-production and production stores. The source
is always the caller's own `environment`, so the direction follows from it:

| Request `environment`      | Copies                                          |
| -------------------------- | ----------------------------------------------- |
| non-production (e.g. `Development`) | `templates/<id>.html` → `templates/production/<id>.html` (promote) |
| `Production`               | `templates/production/<id>.html` → `templates/<id>.html` (copy back down) |

Request body:

```json
{
  "environment": "Development",
  "overwrite": false
}
```

- `environment` — mandatory; the environment the template is copied **from**.
- `overwrite` — defaults to `false`; if the template id already exists in the
  target environment and `overwrite` isn't `true`, the request is rejected.

Response:

```json
{
  "template_id": "invoice",
  "from_environment": "Development",
  "to_environment": "Production",
  "created": true,
  "template": {
    "template_id": "invoice",
    "environment": "Production",
    "name": "Invoice",
    "tags": ["amount", "customer_name", "invoice_date", "invoice_number"],
    "tag_schema": { "amount": null, "customer_name": null, "invoice_date": null, "invoice_number": null }
  }
}
```

`to_environment` is `Production` when promoting, and `non-production` when
copying down (that store is shared by every non-production environment rather
than belonging to one named env). `template` is the migrated template's
metadata as it now reads in the target environment.

Status: `201 Created` when the target copy is new, `200 OK` when an existing
target copy was overwritten. Errors: `404 Not Found` if the template doesn't
exist in the source environment; `409 Conflict` if it already exists in the
target environment and `overwrite` is `false`.

The copy is byte-for-byte and atomic (written to a temp file, then renamed),
and the source copy is left untouched.

### `DELETE /templates/{template_id}`

Delete a template file from one environment's store:

```
DELETE /templates/receipt?environment=Development
```

Responds `204 No Content` on success; `404 Not Found` if `template_id` doesn't
exist in that environment. The other environment's copy, if any, is untouched.

### `POST /generate`

Render a template with tags and return the PDF.

Request body:

```json
{
  "template_id": "invoice",
  "environment": "Production",
  "save_to_disk": false,
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

`save_to_disk` (default `false`) additionally writes a hardcopy of the
rendered PDF to `PDFGEN_OUTPUT_DIR`, named
`<template_id>-<UTC timestamp>-<random hex>.pdf`. The saved path is also
returned in the `X-Hardcopy-Path` response header.

Response: `application/pdf` binary stream (downloadable file).

Missing required tags return `400 Bad Request`; a `template_id` that doesn't
exist **in the requested environment** returns `404 Not Found`.

## Adding a new template

Either:

- Call `POST /templates` (see above) with your template's markup in the
  `html` field and the target `environment` — no file system or server access
  needed, or
- Add a new `.html` file directly under `templates/` (or
  `templates/production/` for production), using `{{ tag_name }}` for any
  dynamic value; it's picked up automatically.

Either way, call `GET /templates?environment=...` to confirm the new
template's id and required tags.

The usual flow is to author and iterate in a non-production environment, then
promote the finished template with
`POST /templates/{template_id}/migrate` once it renders correctly.

## Notes

- Template ids are resolved against an allowlist of existing files in the
  environment's own store; path separators and `..` are rejected to prevent
  path traversal, so an `environment` can never be used to read outside its
  configured directory.
- Tag values are HTML-escaped by Jinja2's autoescaping, so they cannot inject
  markup/script into the generated document.
- A single Chromium instance is started at app startup and reused across
  requests for performance; it is closed on shutdown.
