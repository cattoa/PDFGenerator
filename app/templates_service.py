"""Discovery, validation and rendering helpers for HTML PDF templates.

Templates live as .html files in an environment-specific store: the
``Production`` environment uses ``Settings.production_templates_dir``
(default ``<templates_dir>/production``), and every other environment
shares ``Settings.templates_dir`` (default ``<project root>/templates``).
Callers therefore pass their ``environment`` tag alongside the template id.
A template's public id is its filename without the ``.html`` extension.
Placeholders are plain Jinja2 variables, e.g. ``{{ customer_name }}``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template, TemplateSyntaxError, meta, nodes, select_autoescape

from app.config import (
    NON_PRODUCTION_ENVIRONMENT,
    PRODUCTION_ENVIRONMENT,
    get_settings,
    is_production,
)

# Template ids become filenames, so only allow a conservative charset —
# this also rules out path traversal ("..", "/", "\\") by construction.
_TEMPLATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Defensive upper bound on uploaded template size (HTML templates are text
# and small; this guards against accidental/abusive multi-MB uploads).
MAX_TEMPLATE_BYTES = 512_000

def _templates_dir(environment: str) -> Path:
    return get_settings().templates_dir_for(environment)


def migration_target_environment(environment: str) -> str:
    """Return the environment a migrate request copies *into*.

    Migration always reads from the caller's own environment: a
    non-production caller promotes into production, and a production caller
    copies back down into the shared non-production store.
    """
    return NON_PRODUCTION_ENVIRONMENT if is_production(environment) else PRODUCTION_ENVIRONMENT


@lru_cache(maxsize=None)
def _jinja_environment_for(templates_dir: Path) -> Environment:
    # autoescape is enabled for .html templates so tag values can never inject
    # raw HTML/script markup into the rendered document.
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(enabled_extensions=("html",)),
    )


def _env(environment: str) -> Environment:
    """Return the Jinja2 environment loading from a deployment env's store."""
    return _jinja_environment_for(_templates_dir(environment))


class TemplateNotFoundError(Exception):
    """Raised when a template id does not resolve to a known template file."""


class TemplateValidationError(Exception):
    """Raised when a new template's id or HTML content is invalid."""


class TemplateAlreadyExistsError(Exception):
    """Raised when creating a template whose id already exists and overwrite=False."""


def _resolve_filename(template_id: str, environment: str) -> str:
    # Allowlist check: only simple ids that map to an existing .html file in
    # the environment's templates dir are accepted. This blocks path traversal
    # (e.g. "../secret").
    if not template_id or "/" in template_id or "\\" in template_id or ".." in template_id:
        raise TemplateNotFoundError(f"Template '{template_id}' not found in environment '{environment}'")
    filename = f"{template_id}.html"
    if not (_templates_dir(environment) / filename).is_file():
        raise TemplateNotFoundError(f"Template '{template_id}' not found in environment '{environment}'")
    return filename


def get_template_placeholders(template_id: str, environment: str) -> set[str]:
    """Return the set of top-level tag names referenced by a template."""
    filename = _resolve_filename(template_id, environment)
    source = (_templates_dir(environment) / filename).read_text(encoding="utf-8")
    ast = _env(environment).parse(source)
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


def get_template_tag_schema(template_id: str, environment: str) -> dict[str, TagSchema]:
    """Return each top-level tag name mapped to its required (nested) fields.

    For a tag iterated as an array, e.g. ``{% for line in invoice_lines %}``
    that accesses ``line.description`` and loops over a nested array with
    ``{% for detail in line.invoice_details %}`` / ``detail.sku``, this
    returns ``{"invoice_lines": {"description": None, "invoice_details": {"sku": None}}}``.
    Plain scalar tags map to ``None``. Loop variables themselves (e.g.
    ``line``, ``detail``) are excluded — they are not top-level tags the
    caller needs to supply.
    """
    filename = _resolve_filename(template_id, environment)
    source = (_templates_dir(environment) / filename).read_text(encoding="utf-8")
    ast = _env(environment).parse(source)

    top_level = meta.find_undeclared_variables(ast)
    schema: dict[str, TagSchema] = dict.fromkeys(top_level)

    _scan(ast, {}, top_level, schema)  # type: ignore[arg-type]

    return _sort_schema(schema)


def load_template(template_id: str, environment: str) -> Template:
    """Return the compiled Jinja2 template for a given template id."""
    filename = _resolve_filename(template_id, environment)
    return _env(environment).get_template(filename)


def describe_template(template_id: str, environment: str) -> dict:
    """Return metadata (id, environment, display name, tags, tag schema) for one template."""
    return {
        "template_id": template_id,
        "environment": environment,
        "name": template_id.replace("_", " ").replace("-", " ").title(),
        "tags": sorted(get_template_placeholders(template_id, environment)),
        "tag_schema": get_template_tag_schema(template_id, environment),
    }


def list_templates(environment: str) -> list[dict]:
    """Return metadata (id, display name, required tags) for every template
    in the store backing ``environment``.

    Only files directly in the store are listed, so the nested production
    sub-directory is never reported as part of the non-production store.
    """
    return [describe_template(path.stem, environment) for path in sorted(_templates_dir(environment).glob("*.html"))]


def save_template(template_id: str, html: str, environment: str, *, overwrite: bool = False) -> tuple[dict, bool]:
    """Create (or, with ``overwrite=True``, replace) a template file in the
    store backing ``environment``.

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
        _env(environment).parse(html)
    except TemplateSyntaxError as exc:
        raise TemplateValidationError(f"Invalid Jinja2 template syntax: {exc.message}") from exc

    templates_dir = _templates_dir(environment)
    target = templates_dir / f"{template_id}.html"
    if target.exists() and not overwrite:
        raise TemplateAlreadyExistsError(f"Template '{template_id}' already exists in environment '{environment}'")
    created = not target.exists()

    templates_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(".html.tmp")
    tmp_path.write_text(html, encoding="utf-8")
    tmp_path.replace(target)

    return describe_template(template_id, environment), created


def migrate_template(template_id: str, environment: str, *, overwrite: bool = False) -> tuple[dict, bool]:
    """Copy a template between the non-production and production stores.

    The source is always the caller's own ``environment``: a non-production
    caller promotes ``<templates_dir>/<id>.html`` into the production store,
    and a production caller copies the production copy back down into the
    shared non-production store. Returns the migrated template's metadata (as
    seen in the target environment) and whether the target copy was newly
    created (``True``) versus overwritten (``False``).
    """
    target_environment = migration_target_environment(environment)

    # Raises TemplateNotFoundError if the source environment has no such template.
    filename = _resolve_filename(template_id, environment)
    source = _templates_dir(environment) / filename
    target_dir = _templates_dir(target_environment)
    target = target_dir / filename

    if target.exists() and not overwrite:
        raise TemplateAlreadyExistsError(
            f"Template '{template_id}' already exists in environment '{target_environment}'"
        )
    created = not target.exists()

    target_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(".html.tmp")
    tmp_path.write_bytes(source.read_bytes())
    tmp_path.replace(target)

    return describe_template(template_id, target_environment), created


def delete_template(template_id: str, environment: str) -> None:
    """Delete a template file from an environment's store.

    Raises ``TemplateNotFoundError`` if it doesn't exist there.
    """
    filename = _resolve_filename(template_id, environment)
    (_templates_dir(environment) / filename).unlink()
