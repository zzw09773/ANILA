"""Schema extraction (Action.actions + Valves schema + module metadata).

Two-stage strategy per spec §5.3:

  Stage 1 — **static AST**: ``ast.parse`` the user code, walk the tree
  to pluck the module docstring, the ``Action.actions = [...]`` literal
  list, and any ``Valves(BaseModel)`` field annotations. No execution,
  no side effects, no risk.

  Stage 2 — **dynamic introspection** (only when stage 1 detected
  non-literal patterns): the module is already exec'd by ``runtime.py``
  in a separate-network sandbox container with no egress proxy at all,
  so even if the user's top-level code tries to ``requests.post(...)``
  at import time, the network has no route. We then read
  ``Action.actions`` as a class attribute and call
  ``Valves.model_json_schema()`` to fetch the schema.

The extract container's network isolation is what makes stage 2
acceptable; without it we'd be back at the original "save path =
RCE" hole.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any, Awaitable, Callable


EmitFn = Callable[[dict], Awaitable[None]]


# ── Static AST stage ────────────────────────────────────────────────────


def static_extract(code: str) -> dict[str, Any]:
    """Best-effort static analysis. Returns a partial result + a flag
    telling the caller whether dynamic stage is needed.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {
            "errors": [f"syntax: {exc}"],
            "needs_dynamic": False,
            "metadata": {},
            "actions": [],
            "valves_schema": {},
            "strategy": "ast",
        }

    metadata = _extract_module_metadata(tree)
    actions, actions_dynamic = _extract_actions(tree)
    valves_schema, valves_dynamic = _extract_valves_schema(tree)

    needs_dynamic = actions_dynamic or valves_dynamic
    return {
        "errors": [],
        "needs_dynamic": needs_dynamic,
        "metadata": metadata,
        "actions": actions,
        "valves_schema": valves_schema,
        "strategy": "ast" if not needs_dynamic else "ast+sandbox",
    }


def _extract_module_metadata(tree: ast.Module) -> dict[str, Any]:
    """Parse module docstring as the metadata header.

    The OpenWebUI convention is YAML-ish key/value pairs in a leading
    docstring (``title: ...``, ``version: ...``). We do a shallow split
    that handles ``key: value`` lines so the common cases just work
    without pulling in a YAML dep.
    """
    doc = ast.get_docstring(tree) or ""
    metadata: dict[str, Any] = {}
    for line in doc.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            metadata[key] = value
    return metadata


def _extract_actions(tree: ast.Module) -> tuple[list[dict], bool]:
    """Look for ``class Action: actions = [literal, ...]``.

    Returns ``(actions_list, dynamic_flag)``. ``dynamic_flag=True``
    means we found ``Action`` but its ``actions`` attribute isn't a
    pure literal — caller should fall through to dynamic introspection.
    """
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == "Action"):
            continue
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            targets = [
                t.id for t in item.targets if isinstance(t, ast.Name)
            ]
            if "actions" not in targets:
                continue
            literal = _evaluate_literal(item.value)
            if literal is _DYNAMIC:
                return [], True
            return literal or [], False
        # Found Action class but no `actions = ...` assignment
        return [], False
    # No Action class at all — caller decides whether that's an error
    return [], False


_DYNAMIC = object()


def _evaluate_literal(node: ast.AST) -> Any:
    """Like ``ast.literal_eval`` but returns ``_DYNAMIC`` instead of
    raising, so caller can branch cleanly.
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return _DYNAMIC


def _extract_valves_schema(tree: ast.Module) -> tuple[dict, bool]:
    """Look for ``class Valves(BaseModel)`` and emit a minimal JSON
    Schema from the field annotations.

    Supports the subset needed by 90% of real Functions: ``str``,
    ``int``, ``float``, ``bool``, plus ``Field(default=..., ...)``. If
    the class uses fancier types (Annotated, Generic, custom validators)
    we mark it dynamic and let stage 2 pull the real schema via
    ``model_json_schema()``.
    """
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "Valves":
            continue
        # Only treat it as a Valves model if it explicitly inherits BaseModel
        if not any(
            isinstance(base, ast.Name) and base.id == "BaseModel"
            for base in node.bases
        ):
            return {}, False

        properties: dict[str, dict] = {}
        dynamic = False
        for item in node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(
                item.target, ast.Name
            ):
                continue
            field_name = item.target.id
            type_hint = _ast_type_to_schema(item.annotation)
            if type_hint is None:
                dynamic = True
                continue
            properties[field_name] = type_hint
        return (
            {
                "type": "object",
                "properties": properties,
                "title": "Valves",
            },
            dynamic,
        )

    return {}, False


def _ast_type_to_schema(node: ast.AST) -> dict | None:
    """Map a small subset of Python type annotations to JSON Schema."""
    if isinstance(node, ast.Name):
        return {
            "str": {"type": "string"},
            "int": {"type": "integer"},
            "float": {"type": "number"},
            "bool": {"type": "boolean"},
        }.get(node.id)
    return None


# ── Dynamic stage (called from runtime.py extract mode) ─────────────────


async def run_extract(
    spec,
    user_ns: dict,
    emit: EmitFn,
    emit_error: EmitFn,
    emit_done: EmitFn,
) -> None:
    """Stage 2 dynamic introspection.

    Called *after* ``runtime.py`` has already exec'd the module and
    populated ``user_ns``. Reads attributes; never invokes
    ``Action.action``. Egress is structurally impossible (extract
    container's network has no proxy), so module top-level side
    effects can't exfiltrate.
    """
    action_cls = user_ns.get("Action")
    if action_cls is None:
        await emit_error("missing Action class")
        await emit_done(None)
        return

    actions = list(getattr(action_cls, "actions", []) or [])

    valves_schema: dict = {}
    valves_cls = user_ns.get("Valves")
    if valves_cls is not None:
        try:
            valves_schema = valves_cls.model_json_schema()
        except Exception as exc:
            await emit_error(f"valves schema introspection failed: {exc}")

    metadata = inspect.getdoc(user_ns.get("__name__", None)) or ""

    await emit(
        {
            "type": "extract_result",
            "actions": actions,
            "valves_schema": valves_schema,
            "metadata": _parse_metadata_string(metadata),
            "strategy": "sandbox",
        }
    )
    await emit_done(None)


def _parse_metadata_string(doc: str) -> dict:
    out: dict = {}
    for line in doc.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            out[key] = value
    return out
