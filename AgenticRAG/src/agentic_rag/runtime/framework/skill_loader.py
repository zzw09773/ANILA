"""Markdown-frontmatter skill loader → ``SYNC_TOOL`` Actions.

A "skill" is a Markdown file with YAML frontmatter that the LLM can
invoke as a tool. The frontmatter declares the tool's metadata
(name, description, when_to_use, optional input_schema) and the body
is the prompt the tool returns to the LLM when called.

Why this exists:

- Non-programmers can ship new tools by dropping a file into a
  skills directory — no Python required.
- A growing skill library can be conditionally loaded per request,
  cutting prompt-token cost for irrelevant tools.
- Skills live in version control alongside docs; reviewing a
  skill = reviewing a Markdown file.

File shape::

    ---
    name: summarise_pr
    description: Summarise a GitHub PR for the user.
    when_to_use: User asks "what's in this PR" or pastes a PR URL.
    input_schema:
      type: object
      properties:
        url: {type: string, description: PR URL}
      required: [url]
    ---
    You are summarising a GitHub pull request.

    Step 1: fetch the PR diff.
    Step 2: identify the top three changes by impact.
    Step 3: write a 3-bullet summary the user can scan.

    PR URL: {{ url }}

How invocation works:

When the LLM calls this tool, the Action's handler renders the body
(with ``{{ var }}`` substitutions from ``ctx.params``) and returns it
as the tool result. The LLM then continues its conversation seeing
the rendered body — which is, effectively, an in-context prompt
override that nudges the LLM toward the skill's procedure.

This is intentionally a simple substitution model, NOT Jinja /
mustache. Skill authors who need conditionals or loops are better
off writing a Python Action.

Out of scope (deliberately, lands in v0.3 if needed):

- Shell-callout skill bodies (run a shell command, return stdout)
- Full templating (Jinja, mustache)
- Skill chaining / composition primitives
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    SideEffectClass,
)
from agentic_rag.runtime.framework.exceptions import UserError

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


# ── Skill record ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Skill:
    """One loaded skill — metadata + body template + materialised Action.

    ``when_to_use`` is the discovery hint a SkillRegistry uses to
    decide which skills to surface for a given request. Free text;
    naive substring match by default, can be replaced with an
    LLM-backed ranker by injecting a ``relevance`` callable into
    ``SkillRegistry.relevant_to``.

    ``source_path`` records the file the skill was loaded from —
    useful for "where did this come from?" debugging when several
    skill directories contribute to one registry.
    """

    name: str
    description: str
    when_to_use: str
    body: str
    input_schema: dict[str, Any]
    source_path: str = ""
    action: Action = field(repr=False, default=None)  # type: ignore[assignment]


# ── Loader ───────────────────────────────────────────────────────────


def parse_skill_file(text: str, *, source_path: str = "") -> Skill:
    """Parse one Markdown+frontmatter blob into a Skill.

    Raises ``UserError`` for malformed frontmatter, missing required
    fields, or invalid input_schema. Catches at the loader layer
    convert these into per-file warnings so one bad skill doesn't
    break a whole directory load.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise UserError(
            f"skill {source_path or '<inline>'!r}: missing YAML frontmatter "
            "(expected '---\\n...\\n---' at top of file)"
        )

    yaml_block = match.group("yaml")
    body = match.group("body").strip()
    try:
        front = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError as exc:
        raise UserError(
            f"skill {source_path or '<inline>'!r}: frontmatter YAML invalid: {exc}"
        ) from exc

    if not isinstance(front, dict):
        raise UserError(
            f"skill {source_path or '<inline>'!r}: frontmatter must be a YAML mapping"
        )

    name = front.get("name")
    if not isinstance(name, str) or not name.strip():
        raise UserError(
            f"skill {source_path or '<inline>'!r}: frontmatter.name is required"
        )

    description = str(front.get("description", "")).strip()
    when_to_use = str(front.get("when_to_use", "")).strip()
    input_schema = front.get("input_schema") or {}
    if not isinstance(input_schema, dict):
        raise UserError(
            f"skill {name!r}: input_schema must be a YAML mapping or omitted"
        )

    if not body:
        raise UserError(f"skill {name!r}: body is empty (skill must have content)")

    action = _build_skill_action(
        name=name,
        description=description or f"Skill: {name}",
        body=body,
        input_schema=input_schema,
    )

    return Skill(
        name=name,
        description=description,
        when_to_use=when_to_use,
        body=body,
        input_schema=input_schema,
        source_path=source_path,
        action=action,
    )


def load_skills_from_dir(
    skill_dir: str | Path,
    *,
    pattern: str = "*.md",
    skip_invalid: bool = True,
) -> list[Skill]:
    """Load every Markdown skill matching ``pattern`` under ``skill_dir``.

    Recursive (uses ``rglob``). Returns skills in stable lexicographic
    path order. With ``skip_invalid=True`` (default) malformed files
    log a warning and continue; with ``False`` they raise.
    """
    base = Path(skill_dir)
    if not base.is_dir():
        return []
    skills: list[Skill] = []
    for path in sorted(base.rglob(pattern)):
        try:
            text = path.read_text(encoding="utf-8")
            skills.append(parse_skill_file(text, source_path=str(path)))
        except UserError as exc:
            if skip_invalid:
                logger.warning("skipping invalid skill %s: %s", path, exc)
                continue
            raise
        except OSError as exc:
            if skip_invalid:
                logger.warning("skipping unreadable skill %s: %s", path, exc)
                continue
            raise
    return skills


# ── Action builder ───────────────────────────────────────────────────


_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_]\w*)\s*\}\}")


def _render_body(body: str, params: dict[str, Any]) -> str:
    """``{{ var }}`` substitution. Missing vars render as ``[var]``.

    Deliberately NOT Jinja / mustache — we don't want to depend on a
    template engine for what's essentially "drop these variable values
    into the skill prompt." Authors needing real templating should
    write a Python Action.
    """

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in params:
            return str(params[key])
        return f"[{key}]"

    return _TEMPLATE_VAR_RE.sub(_sub, body)


def _build_skill_action(
    *,
    name: str,
    description: str,
    body: str,
    input_schema: dict[str, Any],
) -> Action:
    """Wrap the skill body as a SYNC_TOOL Action."""

    async def _handler(ctx: ActionContext) -> ActionResult:
        rendered = _render_body(body, ctx.params)
        return ActionResult(output={"skill_body": rendered, "skill_name": name})

    return Action(
        name=name,
        description=description,
        kind=ActionKind.SYNC_TOOL,
        handler=_handler,
        # Skills are prompt-only by design — no external side effects.
        side_effect_class=SideEffectClass.PURE,
        input_schema=input_schema,
    )


__all__ = [
    "Skill",
    "load_skills_from_dir",
    "parse_skill_file",
]
