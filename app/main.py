"""FastAPI app: select a template, supply tags, get back a generated PDF."""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.config import PRODUCTION_ENVIRONMENT, get_settings

# A tag value can be a scalar, or an array/object of tag values — this lets
# templates loop over structured data (e.g. a list of invoice line items)
# with Jinja2's {% for %}. Declared with the `type` statement (not a plain
# assignment) so pydantic can build a recursive schema without infinite
# recursion.
type TagValue = str | int | float | bool | None | list[TagValue] | dict[str, TagValue]

# A tag's schema is None for a plain scalar tag, or a dict of nested field
# names (recursively) for a tag iterated as an array/object, e.g.
# {"invoice_lines": {"description": None, "invoice_detail": {"sku": None}}}.
type TagSchema = None | dict[str, TagSchema]

from app.pdf_service import PdfRenderer
from app.templates_service import (
    TemplateAlreadyExistsError,
    TemplateNotFoundError,
    TemplateValidationError,
    delete_template,
    get_template_placeholders,
    get_template_tag_schema,
    list_templates,
    load_template,
    migrate_template,
    migration_target_environment,
    read_template_source,
    save_template,
)

_ENVIRONMENT_DESCRIPTION = (
    f"Deployment environment this request targets (mandatory). '{PRODUCTION_ENVIRONMENT}' "
    "(case-insensitive) reads and writes templates in the production template store; "
    "every other value (e.g. Development, Test, UAT) uses the shared non-production store."
)

# Mandatory `environment` tag for requests with no body (GET/DELETE), where it
# has to travel as a query parameter instead of a JSON field.
EnvironmentQuery = Annotated[str, Query(min_length=1, description=_ENVIRONMENT_DESCRIPTION)]


def _environment_field() -> str:
    """The mandatory `environment` tag as a request-body field."""
    return Field(..., min_length=1, description=_ENVIRONMENT_DESCRIPTION)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    # Both template stores exist up front, so listing an environment that has
    # no templates yet is an empty response rather than a missing directory.
    settings.templates_dir.mkdir(parents=True, exist_ok=True)
    settings.templates_dir_for(PRODUCTION_ENVIRONMENT).mkdir(parents=True, exist_ok=True)
    renderer = PdfRenderer()
    await renderer.start()
    app.state.pdf_renderer = renderer
    try:
        yield
    finally:
        await renderer.stop()


app = FastAPI(
    title="PDF Generator API",
    description="Consume tags to fill a selected HTML template and generate a PDF.",
    version="2.0.0",
    lifespan=lifespan,
)


def get_pdf_renderer(request: Request) -> PdfRenderer:
    return request.app.state.pdf_renderer


class TemplateSummary(BaseModel):
    template_id: str
    environment: str
    name: str
    tags: list[str]
    tag_schema: dict[str, TagSchema]


class TemplateSource(BaseModel):
    template_id: str
    environment: str
    html: str


class GenerateRequest(BaseModel):
    template_id: str = Field(..., description="Id of the template to use (its filename without .html)")
    environment: str = _environment_field()
    tags: dict[str, TagValue] = Field(
        default_factory=dict,
        description=(
            "Tags substituted into the template's {{ placeholders }}. Values may be "
            "strings, numbers, booleans, or arrays/objects — e.g. a list of invoice "
            "line items — for templates that loop over them with Jinja2 {% for %}."
        ),
    )
    save_to_disk: bool = Field(
        False,
        description="If true, also write a hardcopy of the generated PDF to the configured output directory",
    )


class GenerateResponse(BaseModel):
    template_id: str
    environment: str
    filename: str = Field(..., description="Suggested filename for the PDF, e.g. invoice.pdf")
    media_type: str = Field("application/pdf", description="Media type of the decoded content")
    size_bytes: int = Field(..., description="Size of the decoded PDF in bytes")
    pdf_base64: str = Field(..., description="The generated PDF, base64-encoded — decode this to get the file")
    hardcopy_path: str | None = Field(
        None,
        description="Where the hardcopy was written when save_to_disk was true; null otherwise",
    )


class CreateTemplateRequest(BaseModel):
    template_id: str = Field(
        ..., description="Id for the new template — letters, numbers, underscores and hyphens only"
    )
    environment: str = _environment_field()
    html: str = Field(..., description="Full HTML content of the template, using Jinja2 {{ tag }} placeholders")
    overwrite: bool = Field(False, description="If true, replace an existing template with the same id")


class MigrateTemplateRequest(BaseModel):
    environment: str = _environment_field()
    overwrite: bool = Field(
        False,
        description="If true, replace an existing template with the same id in the target environment",
    )


class MigrateTemplateResponse(BaseModel):
    template_id: str
    from_environment: str
    to_environment: str
    created: bool
    template: TemplateSummary


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/templates", response_model=list[TemplateSummary])
def get_templates(environment: EnvironmentQuery) -> list[dict]:
    return list_templates(environment)


@app.get("/templates/{template_id}", response_model=TemplateSource)
def get_template_html(template_id: str, environment: EnvironmentQuery) -> dict:
    """Return one template's raw HTML source.

    `html` is the stored file verbatim — Jinja2 `{{ tag }}` placeholders
    unrendered — so it can be edited and posted straight back to
    `POST /templates` with `overwrite: true`. Use `/generate` for a filled-in PDF.
    """
    try:
        html = read_template_source(template_id, environment)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"template_id": template_id, "environment": environment, "html": html}


@app.post("/templates", response_model=TemplateSummary)
def create_template(request: CreateTemplateRequest, response: Response) -> dict:
    try:
        result, created = save_template(
            request.template_id, request.html, request.environment, overwrite=request.overwrite
        )
    except TemplateAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TemplateValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return result


@app.get("/templates/{template_id}/tags")
def get_template_tags(template_id: str, environment: EnvironmentQuery) -> dict:
    try:
        placeholders = get_template_placeholders(template_id, environment)
        tag_schema = get_template_tag_schema(template_id, environment)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "template_id": template_id,
        "environment": environment,
        "tags": sorted(placeholders),
        "tag_schema": tag_schema,
    }


@app.post("/templates/{template_id}/migrate", response_model=MigrateTemplateResponse)
def migrate_template_endpoint(template_id: str, request: MigrateTemplateRequest, response: Response) -> dict:
    """Copy a template out of the caller's environment into the other store.

    A non-production caller promotes their template into production; a
    production caller copies the production template back down into the
    shared non-production store.
    """
    try:
        template, created = migrate_template(template_id, request.environment, overwrite=request.overwrite)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TemplateAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return {
        "template_id": template_id,
        "from_environment": request.environment,
        "to_environment": migration_target_environment(request.environment),
        "created": created,
        "template": template,
    }


@app.delete("/templates/{template_id}", status_code=204)
def delete_template_endpoint(template_id: str, environment: EnvironmentQuery) -> Response:
    try:
        delete_template(template_id, environment)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@app.post("/generate", response_model=GenerateResponse)
async def generate_pdf(
    request: GenerateRequest,
    renderer: PdfRenderer = Depends(get_pdf_renderer),
) -> dict:
    """Render a template with the supplied tags and return the PDF as base64 JSON.

    The PDF arrives in `pdf_base64` rather than as a binary body, so callers
    decode that field to get the file. With `save_to_disk: true` the hardcopy's
    location comes back in `hardcopy_path`.
    """
    try:
        placeholders = get_template_placeholders(request.template_id, request.environment)
        template = load_template(request.template_id, request.environment)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    missing = placeholders - request.tags.keys()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required tags: {', '.join(sorted(missing))}",
        )

    html = template.render(**request.tags)
    pdf_bytes = await renderer.render(html)

    hardcopy_path: Path | None = None
    if request.save_to_disk:
        settings = get_settings()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        hardcopy_name = f"{request.template_id}-{timestamp}-{uuid.uuid4().hex[:8]}.pdf"
        hardcopy_path = settings.output_dir / hardcopy_name
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        hardcopy_path.write_bytes(pdf_bytes)

    return {
        "template_id": request.template_id,
        "environment": request.environment,
        "filename": f"{request.template_id}.pdf",
        "media_type": "application/pdf",
        "size_bytes": len(pdf_bytes),
        "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        "hardcopy_path": None if hardcopy_path is None else str(hardcopy_path),
    }
