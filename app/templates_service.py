"""Discovery, validation and rendering helpers for HTML PDF templates.

Templates live as .html files under the top-level ``templates`` directory.
A template's public id is its filename without the ``.html`` extension.
Placeholders are plain Jinja2 variables, e.g. ``{{ customer_name }}``.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template, TemplateSyntaxError, meta, nodes, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Template ids become filenames, so only allow a conservative charset —
# this also rules out path traversal ("..", "/", "\\") by construction.
_TEMPLATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Defensive upper bound on uploaded template size (HTML templates are text
# and small; this guards against accidental/abusive multi-MB uploads).
MAX_TEMPLATE_BYTES = 512_000

# autoescape is enabled for .html templates so tag values can never inject
# raw HTML/script markup into the rendered document.
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html",)),
)


class TemplateNotFoundError(Exception):
    """Raised when a template id does not resolve to a known template file."""


class TemplateValidationError(Exception):
    """Raised when a new template's id or HTML content is invalid."""


class TemplateAlreadyExistsError(Exception):
    """Raised when creating a template whose id already exists and overwrite=False."""


def _resolve_filename(template_id: str) -> str:
    # Allowlist check: only simple ids that map to an existing .html file in
    # TEMPLATES_DIR are accepted. This blocks path traversal (e.g. "../secret").
    if not template_id or "/" in template_id or "\\" in template_id or ".." in template_id:
        raise TemplateNotFoundError(f"Template '{template_id}' not found")
    filename = f"{template_id}.html"
    if not (TEMPLATES_DIR / filename).is_file():
        raise TemplateNotFoundError(f"Template '{template_id}' not found")
    return filename


def get_template_placeholders(template_id: str) -> set[str]:
    """Return the set of top-level tag names referenced by a template."""
    filename = _resolve_filename(template_id)
    source = (TEMPLATES_DIR / filename).read_text(encoding="utf-8")
    ast = _env.parse(source)
    return meta.find_undeclared_variables(ast)


def _resolve_path(
    node: nodes.Node, loop_scope: dict[str, tuple[str, ...]], top_level: set[str]
) -> tuple[str, ...] | None:
    """Resolve an expression to the schema path it refers to.

    A bare top-level tag name resolves to a 1-element path, e.g. ``invoice_lines``
    -> ``("invoice_lines",)``. A Getattr/Getitem chain rooted at a known loop
    variable extends that variable's own path, e.g. ``line.invoice_details``
    where ``line`` is bound to ``("invoice_lines",)`` resolves to
    ``("invoice_lines", "invoice_details")`` — this is what lets an array tag
    be nested inside another array tag. Returns ``None`` for expressions that
    don't resolve to a known tag/loop var (literals, filters, calls, ...).
    """
    if isinstance(node, nodes.Name):
        if node.name in loop_scope:
            return loop_scope[node.name]
        if node.name in top_level:
            return (node.name,)
        return None
    if isinstance(node, nodes.Getattr):
        base = _resolve_path(node.node, loop_scope, top_level)
        return None if base is None else (*base, node.attr)
    if isinstance(node, nodes.Getitem):
        base = _resolve_path(node.node, loop_scope, top_level)
        if base is None:
            return None
        arg = node.arg
        if isinstance(arg, nodes.Const) and isinstance(arg.value, str):
            return (*base, arg.value)
        return None
    return None


def _insert_path(schema: dict[str, object], path: tuple[str, ...]) -> None:
    """Insert a field path into a nested schema dict, e.g. inserting
    ``("invoice_lines", "invoice_details", "sku")`` produces
    ``{"invoice_lines": {"invoice_details": {"sku": None}}}``. Safe to call
    with paths in any order/depth for the same field (a leaf is upgraded to a
    nested dict the first time a longer path for it is seen), so both nested
    objects and arrays nested inside arrays share one representation.
    """
    key, *rest = path
    if not rest:
        schema.setdefault(key, None)
        return
    if not isinstance(schema.get(key), dict):
        schema[key] = {}
    _insert_path(schema[key], tuple(rest))  # type: ignore[arg-type]


type TagSchema = None | dict[str, "TagSchema"]


def _sort_schema(schema: dict[str, TagSchema]) -> dict[str, TagSchema]:
    """Recursively sort a schema dict's keys for stable, readable output."""
    return {key: _sort_schema(value) if isinstance(value, dict) else value for key, value in sorted(schema.items())}


def _scan(
    node: nodes.Node,
    loop_scope: dict[str, tuple[str, ...]],
    top_level: set[str],
    schema: dict[str, object],
) -> None:
    """Recursively walk the template AST, tracking which loop variables are
    currently bound to which schema path, to build a (possibly deeply
    nested) tag schema — including arrays nested inside other arrays.
    """
    if isinstance(node, nodes.For) and isinstance(node.target, nodes.Name):
        path = _resolve_path(node.iter, loop_scope, top_level)
        if path is not None:
            _insert_path(schema, path)
            child_scope = {**loop_scope, node.target.name: path}
            for sub in (*node.body, *node.else_):
                _scan(sub, child_scope, top_level, schema)
            if node.test is not None:
                _scan(node.test, child_scope, top_level, schema)
            return

    if isinstance(node, (nodes.Getattr, nodes.Getitem)):
        path = _resolve_path(node, loop_scope, top_level)
        if path is not None and len(path) > 1:
            _insert_path(schema, path)

    for child in node.iter_child_nodes():
        _scan(child, loop_scope, top_level, schema)


def get_template_tag_schema(template_id: str) -> dict[str, TagSchema]:
    """Return each top-level tag name mapped to its required (nested) fields.

    For a tag iterated as an array, e.g. ``{% for line in invoice_lines %}``
    that accesses ``line.description`` and loops over a nested array with
    ``{% for detail in line.invoice_details %}`` / ``detail.sku``, this
    returns ``{"invoice_lines": {"description": None, "invoice_details": {"sku": None}}}``.
    Plain scalar tags map to ``None``. Loop variables themselves (e.g.
    ``line``, ``detail``) are excluded — they are not top-level tags the
    caller needs to supply.
    """
    filename = _resolve_filename(template_id)
    source = (TEMPLATES_DIR / filename).read_text(encoding="utf-8")
    ast = _env.parse(source)

    top_level = meta.find_undeclared_variables(ast)
    schema: dict[str, TagSchema] = dict.fromkeys(top_level)

    _scan(ast, {}, top_level, schema)  # type: ignore[arg-type]

    return _sort_schema(schema)


def load_template(template_id: str) -> Template:
    """Return the compiled Jinja2 template for a given template id."""
    filename = _resolve_filename(template_id)
    return _env.get_template(filename)


def describe_template(template_id: str) -> dict:
    """Return metadata (id, display name, tags, tag schema) for one template."""
    return {
        "template_id": template_id,
        "name": template_id.replace("_", " ").replace("-", " ").title(),
        "tags": sorted(get_template_placeholders(template_id)),
        "tag_schema": get_template_tag_schema(template_id),
    }


def list_templates() -> list[dict]:
    """Return metadata (id, display name, required tags) for every template."""
    return [describe_template(path.stem) for path in sorted(TEMPLATES_DIR.glob("*.html"))]


def save_template(template_id: str, html: str, *, overwrite: bool = False) -> tuple[dict, bool]:
    """Create (or, with ``overwrite=True``, replace) a template file.

    Validates the template id (safe charset only) and that ``html`` is
    non-empty, within the size limit, and syntactically valid Jinja2, then
    writes it atomically (write to a temp file, then rename). Returns a
    tuple of the new template's metadata and whether it was newly created
    (``True``) versus an existing template being overwritten (``False``).
    """
    if not template_id or not _TEMPLATE_ID_PATTERN.fullmatch(template_id):
        raise TemplateValidationError(
            "template_id must be a non-empty string of letters, numbers, underscores and hyphens only"
        )

    if not html or not html.strip():
        raise TemplateValidationError("Template HTML content must not be empty")

    if len(html.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise TemplateValidationError(f"Template HTML content exceeds the {MAX_TEMPLATE_BYTES}-byte limit")

    try:
        _env.parse(html)
    except TemplateSyntaxError as exc:
        raise TemplateValidationError(f"Invalid Jinja2 template syntax: {exc.message}") from exc

    target = TEMPLATES_DIR / f"{template_id}.html"
    if target.exists() and not overwrite:
        raise TemplateAlreadyExistsError(f"Template '{template_id}' already exists")
    created = not target.exists()

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(".html.tmp")
    tmp_path.write_text(html, encoding="utf-8")
    tmp_path.replace(target)

    return describe_template(template_id), created
