"""``@tool`` decorator — auto-generate Action from a Python function.

Authors used to write::

    async def search_handler(ctx):
        ...
    search = Action(
        name="search",
        description="Vector search over user documents",
        kind=ActionKind.SYNC_TOOL,
        handler=search_handler,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    )

With ``@tool`` they write::

    @tool
    async def search(query: str, top_k: int = 5) -> dict:
        '''Vector search over user documents.

        Args:
            query: Natural language query.
            top_k: Number of results to return.
        '''
        ...

The decorator introspects the signature, parses the docstring's
``Args:`` block (Google style), and produces a complete ``Action`` —
no schema by hand, no name by hand.

Scope:
- Supported types: ``str``, ``int``, ``float``, ``bool``, ``list``,
  ``dict``, ``None``, ``Annotated[T, "description"]``, ``Optional[T]``,
  ``T | None``.
- Defaults flow into the schema's ``default`` field.
- The first parameter is treated as ``ActionContext`` and skipped from
  the schema (the framework injects it). Detection is by name (``ctx``
  / ``context``) OR by annotation (``ActionContext``).
- Docstring parsing is pragmatic: Google-style ``Args:`` block where
  each line matches ``  name: description``. Numpy / Sphinx styles
  unsupported; if your tool needs them, write the schema by hand.

Out of scope (deliberately): nested object schemas, enum constraints,
string format validators, regex patterns. If you need those, build
the ``input_schema`` dict manually — the decorator is a convenience,
not a sealed-off API.
"""

from __future__ import annotations

import inspect
import re
import textwrap
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    CostEstimate,
    SideEffectClass,
)
from agentic_rag.runtime.framework.exceptions import UserError


# ── Type → JSON Schema mapping ────────────────────────────────────────


_PY_TYPE_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _json_type_for(annotation: Any) -> dict[str, Any]:
    """Map a Python type annotation to a JSON schema fragment.

    Handles:
      - bare types (``str``, ``int``, ``list``, etc.)
      - ``Annotated[T, "description"]`` (description extracted)
      - ``Optional[T]`` and ``T | None`` (rendered as ``[T-type, "null"]``)
      - ``list[T]`` (``{type: array, items: {type: T}}``)

    Unknown / complex types fall back to an empty fragment ``{}`` so
    the decorator never blocks on author-defined types it can't model.
    """
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Annotated[T, "desc", ...]
    if origin is not None and getattr(annotation, "__metadata__", None):
        base_schema = _json_type_for(annotation.__origin__)
        for meta in annotation.__metadata__:
            if isinstance(meta, str):
                base_schema["description"] = meta
                break
        return base_schema

    # Optional[T] / T | None / Union
    if origin is Union or _is_uniontype(origin):
        non_null = [a for a in args if a is not type(None)]
        nullable = type(None) in args
        if len(non_null) == 1:
            base = _json_type_for(non_null[0])
            if nullable and "type" in base:
                t = base["type"]
                base["type"] = [t, "null"] if isinstance(t, str) else list(t) + ["null"]
            return base
        # Multiple non-None types → list of types; lossy but safe.
        types = []
        for a in non_null:
            inner = _json_type_for(a)
            t = inner.get("type")
            if isinstance(t, str):
                types.append(t)
        if nullable:
            types.append("null")
        return {"type": types} if types else {}

    # list[T]
    if origin in (list, tuple):
        inner = _json_type_for(args[0]) if args else {}
        return {"type": "array", "items": inner or {}}

    # dict[K, V]
    if origin is dict:
        return {"type": "object"}

    # Bare type
    if isinstance(annotation, type):
        json_type = _PY_TYPE_TO_JSON.get(annotation)
        if json_type:
            return {"type": json_type}

    # Anything else (TypedDict, Pydantic model, BaseModel subclass, etc.)
    # → leave open. Authors can layer Pydantic on top inside the handler.
    return {}


def _is_uniontype(origin: Any) -> bool:
    """``X | Y`` syntax produces ``types.UnionType`` on 3.10+; treat
    that the same as ``typing.Union``."""
    try:
        from types import UnionType

        return origin is UnionType
    except ImportError:
        return False


# ── Docstring parsing (Google style) ──────────────────────────────────


_GOOGLE_ARGS_HEADER = re.compile(r"^\s*Args?:\s*$", re.MULTILINE)
_GOOGLE_PARAM_LINE = re.compile(r"^\s*([a-zA-Z_]\w*)\s*(?:\([^)]*\))?\s*:\s*(.+)$")


def _parse_docstring(docstring: str | None) -> tuple[str, dict[str, str]]:
    """Pull the leading summary paragraph and ``Args:`` map from a docstring.

    Returns ``(summary, {param_name: description})``. Both default to
    empty if the docstring is missing / unparseable.
    """
    if not docstring:
        return "", {}

    cleaned = textwrap.dedent(docstring).strip()
    summary_lines: list[str] = []
    for line in cleaned.splitlines():
        if not line.strip():
            break
        # Stop if we've hit a Google section header (Args:, Returns:, etc.)
        if line.rstrip().endswith(":") and len(line.strip().split()) <= 2:
            break
        summary_lines.append(line)
    summary = " ".join(s.strip() for s in summary_lines).strip()

    # Locate Args: block
    arg_map: dict[str, str] = {}
    args_match = _GOOGLE_ARGS_HEADER.search(cleaned)
    if args_match:
        body = cleaned[args_match.end():]
        raw_lines = body.splitlines()
        # Strip leading blank lines (the newline immediately after the
        # ``Args:`` header creates one). The block ends at the first
        # blank line *after* we've started collecting content.
        block_lines: list[str] = []
        started = False
        for line in raw_lines:
            stripped = line.strip()
            if not stripped:
                if started:
                    break
                continue
            started = True
            block_lines.append(line)
        current_name: str | None = None
        for line in block_lines:
            match = _GOOGLE_PARAM_LINE.match(line)
            if match:
                current_name, desc = match.group(1), match.group(2).strip()
                arg_map[current_name] = desc
            elif current_name is not None:
                # Continuation line for the previous param.
                arg_map[current_name] = (
                    arg_map[current_name] + " " + line.strip()
                ).strip()

    return summary, arg_map


# ── Public decorator ──────────────────────────────────────────────────


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    kind: ActionKind = ActionKind.SYNC_TOOL,
    side_effect_class: SideEffectClass = SideEffectClass.PURE,
    cost_estimate: CostEstimate | None = None,
    additional_properties: bool = False,
) -> Action | Callable[[Callable[..., Any]], Action]:
    """Decorate a function to produce an ``Action``.

    Usage with parens (``@tool(name="search")``) or without
    (``@tool`` plain). Both styles work.

    The decorated function:
      - First parameter (``ctx`` / ``context`` by name or
        ``ActionContext`` by annotation) is the framework-injected
        context; it does NOT appear in the generated schema.
      - Remaining parameters become schema properties; defaults flow
        through; ``Annotated[T, "desc"]`` provides per-param description
        when the docstring doesn't.
      - Return value is wrapped: dict / list / scalar → ``ActionResult``
        with ``output=<that>``; ``ActionResult`` returned as-is.

    ``additional_properties=False`` (default) means the schema rejects
    extra fields the LLM might invent. Set ``True`` for tools that
    intentionally accept unknown kwargs.
    """

    def _build(func: Callable[..., Any]) -> Action:
        if not inspect.iscoroutinefunction(func):
            raise UserError(
                f"@tool target {func.__name__!r} must be async (coroutine function)"
            )

        # eval_str=True resolves PEP-563 string annotations (which appear
        # whenever the decorated module has ``from __future__ import
        # annotations``) into actual types. Without this, every annotation
        # would be a string and _json_type_for would fall through to
        # the empty-fragment fallback.
        try:
            sig = inspect.signature(func, eval_str=True)
        except (NameError, AttributeError):
            # Annotation references a name that isn't importable at the
            # decoration site (forward-ref to a class defined below, etc.).
            # Fall back to unresolved annotations; schema fragments for
            # those params will be empty but the tool still works.
            sig = inspect.signature(func)
        params = list(sig.parameters.values())
        if not params:
            raise UserError(
                f"@tool target {func.__name__!r} must take at least one parameter "
                "(ActionContext)"
            )

        # First param is the context — by name or annotation.
        ctx_param = params[0]
        if not _looks_like_context_param(ctx_param):
            raise UserError(
                f"@tool {func.__name__!r}: first parameter must be ActionContext "
                f"(named ctx/context or annotated ActionContext); got {ctx_param.name!r}"
            )
        schema_params = params[1:]

        summary, arg_descriptions = _parse_docstring(func.__doc__)

        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for p in schema_params:
            if p.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                # *args / **kwargs in a tool signature don't translate
                # cleanly to JSON schema. Skip and let the LLM-emitted
                # extras flow through if additional_properties=True.
                continue
            schema_fragment = _json_type_for(p.annotation) if p.annotation is not inspect.Parameter.empty else {}
            # Docstring description overrides any Annotated[..., "desc"]
            # because the docstring is usually maintained more carefully
            # than parameter annotations.
            doc_desc = arg_descriptions.get(p.name)
            if doc_desc:
                schema_fragment["description"] = doc_desc
            if p.default is not inspect.Parameter.empty:
                schema_fragment["default"] = p.default
            else:
                required.append(p.name)
            properties[p.name] = schema_fragment

        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": additional_properties,
        }
        if required:
            input_schema["required"] = required

        async def _handler(ctx: ActionContext) -> ActionResult:
            try:
                kwargs = _bind_kwargs(ctx.params, schema_params)
            except TypeError as exc:
                # Defensive: the runner pre-validates input_schema, but
                # if a custom middleware mutated params we may still
                # land here. Surface as a recoverable error.
                return ActionResult(error=f"argument-binding failed: {exc}")

            try:
                output = await func(ctx, **kwargs)
            except Exception as exc:  # noqa: BLE001
                return ActionResult(error=f"{type(exc).__name__}: {exc}")

            if isinstance(output, ActionResult):
                return output
            return ActionResult(output=output)

        return Action(
            name=name or func.__name__,
            description=description or summary or func.__name__,
            kind=kind,
            handler=_handler,
            input_schema=input_schema,
            side_effect_class=side_effect_class,
            cost_estimate=cost_estimate or CostEstimate(),
        )

    # Bare ``@tool`` (no parens) — fn is the decorated function.
    if fn is not None and callable(fn):
        return _build(fn)
    # Parenthesised ``@tool(...)`` — return the decorator.
    return _build


def _looks_like_context_param(param: inspect.Parameter) -> bool:
    """Heuristic: is this the framework-injected context param?

    Three accepted shapes:
      1. Parameter named ``ctx`` or ``context``
      2. Annotated with the ``ActionContext`` class directly (real type)
      3. Annotated with the string ``"ActionContext"`` (PEP 563 / from
         __future__ import annotations defers annotation evaluation,
         so we get a string instead of the class)
    """
    if param.name in ("ctx", "context"):
        return True
    annot = param.annotation
    if annot is ActionContext:
        return True
    annot_name = getattr(annot, "__name__", None)
    if annot_name == "ActionContext":
        return True
    # PEP 563 string annotation
    if isinstance(annot, str):
        # Match either bare "ActionContext" or any qualified path that
        # ends in ".ActionContext" (e.g., "framework.action.ActionContext").
        return annot == "ActionContext" or annot.endswith(".ActionContext")
    return False


def _bind_kwargs(
    params: dict[str, Any], schema_params: list[inspect.Parameter]
) -> dict[str, Any]:
    """Project ``params`` (the LLM-supplied dict) onto the function's kwargs.

    Defaults from the signature are applied for missing keys; extra keys
    from ``params`` are forwarded only when the schema's
    ``additionalProperties`` was ``True`` — the runner-level validator
    rejects them otherwise, so by the time we land here only declared
    kwargs are present.
    """
    out: dict[str, Any] = {}
    declared_names = {p.name for p in schema_params}
    for p in schema_params:
        if p.name in params:
            out[p.name] = params[p.name]
        elif p.default is not inspect.Parameter.empty:
            out[p.name] = p.default
        # else: omit, let the function raise TypeError if it really
        # needed the value (validator should have caught this earlier).
    # Forward unknown keys (only reachable when additionalProperties is True).
    for key, value in params.items():
        if key not in declared_names:
            out[key] = value
    return out


__all__ = ["tool"]
