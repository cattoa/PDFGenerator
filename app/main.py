"""FastAPI app: select a template, supply tags, get back a generated PDF."""

from __future__ import annotations

import io
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings

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
    save_template,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_settings().output_dir.mkdir(parents=True, exist_ok=True)
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
    version="1.0.0",
    lifespan=lifespan,
)


def get_pdf_renderer(request: Request) -> PdfRenderer:
    return request.app.state.pdf_renderer


class TemplateSummary(BaseModel):
    template_id: str
    name: str
    tags: list[str]
    tag_schema: dict[str, TagSchema]


class GenerateRequest(BaseModel):
    template_id: str = Field(..., description="Id of the template to use (its filename without .html)")
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


class CreateTemplateRequest(BaseModel):
    template_id: str = Field(
        ..., description="Id for the new template — letters, numbers, underscores and hyphens only"
    )
    html: str = Field(..., description="Full HTML content of the template, using Jinja2 {{ tag }} placeholders")
    overwrite: bool = Field(False, description="If true, replace an existing template with the same id")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/templates", response_model=list[TemplateSummary])
def get_templates() -> list[dict]:
    return list_templates()


@app.post("/templates", response_model=TemplateSummary)
def create_template(request: CreateTemplateRequest, response: Response) -> dict:
    try:
        result, created = save_template(request.template_id, request.html, overwrite=request.overwrite)
    except TemplateAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TemplateValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return result


@app.get("/templates/{template_id}/tags")
def get_template_tags(template_id: str) -> dict:
    try:
        placeholders = get_template_placeholders(template_id)
        tag_schema = get_template_tag_schema(template_id)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "template_id": template_id,
        "tags": sorted(placeholders),
        "tag_schema": tag_schema,
    }


@app.delete("/templates/{template_id}", status_code=204)
def delete_template_endpoint(template_id: str) -> Response:
    try:
        delete_template(template_id)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@app.post("/generate")
async def generate_pdf(
    request: GenerateRequest,
    renderer: PdfRenderer = Depends(get_pdf_renderer),
) -> StreamingResponse:
    try:
        placeholders = get_template_placeholders(request.template_id)
        template = load_template(request.template_id)
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

    filename = f"{request.template_id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    if request.save_to_disk:
        settings = get_settings()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        hardcopy_name = f"{request.template_id}-{timestamp}-{uuid.uuid4().hex[:8]}.pdf"
        hardcopy_path = settings.output_dir / hardcopy_name
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        hardcopy_path.write_bytes(pdf_bytes)
        headers["X-Hardcopy-Path"] = str(hardcopy_path)

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers=headers,
    )
