"""``SkillRegistry`` — conditional discovery of loaded skills.

A registry holds N loaded ``Skill`` objects (typically from
``load_skills_from_dir``) and answers two questions per request:

1. Which skills are RELEVANT to the user's query? (filter)
2. Of those, which ``Action`` objects should be added to the agent?

Default ranker: token-overlap between query and ``when_to_use``
substring. Naive but works for small (≤30) skill libraries. Larger
libraries should inject an LLM-side ranker via the ``relevance``
callable — same shape as the relevance_selector pattern from
``memory/relevance_selector.py``.

Why filter at all rather than dump every skill into the prompt:

- Each skill's frontmatter (description + input_schema) costs tokens.
  Twenty skills × 200 tokens each = 4K tokens of overhead the LLM
  pays per turn whether or not it uses any of them.
- LLMs distract more easily when the tool list is long. A focused
  3-skill list pushes the model toward the right tool faster than
  a 30-skill smorgasbord.

Use::

    skills = load_skills_from_dir("/var/agent/skills/")
    registry = SkillRegistry(skills)
    relevant_actions = registry.actions_for(query="summarise this PR")
    agent = Agent(name=..., actions=tuple([
        *built_in_actions,
        *relevant_actions,
    ]), ...)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Optional

from agentic_rag.runtime.framework.action import Action
from agentic_rag.runtime.framework.skill_loader import Skill

logger = logging.getLogger(__name__)


# Async ranker: takes (query, candidate skills) → ranked subset.
# Replaces the default substring ranker when LLM-side ranking is desired.
RelevanceRanker = Callable[
    [str, list[Skill]], Awaitable[list[Skill]]
]


# ── Registry ─────────────────────────────────────────────────────────


@dataclass
class SkillRegistry:
    """Ordered collection of skills with conditional discovery.

    Mutable: ``add()`` / ``remove()`` operate on the live registry,
    ``add_all()`` bulk-loads. Construction-time ``skills=`` seeds.

    Names must be unique within a registry. Re-registering raises
    ``ValueError`` so silent shadowing doesn't bite later.
    """

    skills: list[Skill]
    _by_name: dict[str, Skill]

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self.skills = []
        self._by_name = {}
        for s in skills:
            self.add(s)

    def add(self, skill: Skill) -> None:
        if skill.name in self._by_name:
            raise ValueError(
                f"SkillRegistry already has a skill named {skill.name!r} "
                f"(from {self._by_name[skill.name].source_path or '<inline>'})"
            )
        self._by_name[skill.name] = skill
        self.skills.append(skill)

    def add_all(self, skills: Iterable[Skill]) -> None:
        for s in skills:
            self.add(s)

    def remove(self, name: str) -> bool:
        skill = self._by_name.pop(name, None)
        if skill is None:
            return False
        self.skills.remove(skill)
        return True

    def get(self, name: str) -> Skill | None:
        return self._by_name.get(name)

    def __len__(self) -> int:
        return len(self.skills)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name

    def all_actions(self) -> list[Action]:
        """Return the Action of every loaded skill, in registration order.

        Use when you DON'T want filtering — small skill libraries
        (≤5 skills) often don't need it.
        """
        return [s.action for s in self.skills]

    # ── Discovery ────────────────────────────────────────────────────

    def relevant_to(
        self,
        query: str,
        *,
        limit: int = 5,
        ranker: Optional[RelevanceRanker] = None,
    ) -> list[Skill]:
        """Return skills relevant to ``query``, ranked best-first.

        With a custom ``ranker`` (typically an LLM-backed one), the
        query → candidate list goes through the ranker for true
        semantic matching. Without it, falls back to the substring
        ranker — cheap, deterministic, no LLM calls.

        Note: this method is async-friendly when a ranker is supplied
        (return an awaitable wrapper). For now we keep it sync because
        the default ranker is sync and most callers want the simple
        path. ``aelevant_to`` is the async variant.
        """
        if ranker is not None:
            raise ValueError(
                "Sync relevant_to() does not support an async ranker; "
                "use the async aelevant_to() method."
            )
        return _substring_rank(query, self.skills)[:limit]

    async def aelevant_to(
        self,
        query: str,
        *,
        limit: int = 5,
        ranker: Optional[RelevanceRanker] = None,
    ) -> list[Skill]:
        """Async variant — supports an LLM-backed ``ranker``.

        Identical behaviour to ``relevant_to`` when no ranker is
        supplied (delegates to the substring ranker).
        """
        if ranker is None:
            return _substring_rank(query, self.skills)[:limit]
        try:
            ranked = await ranker(query, list(self.skills))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SkillRegistry.aelevant_to: ranker failed (%s); falling back to substring",
                exc,
            )
            return _substring_rank(query, self.skills)[:limit]
        return ranked[:limit]

    def actions_for(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[Action]:
        """Convenience: ``relevant_to`` + extract ``.action`` from each."""
        return [s.action for s in self.relevant_to(query, limit=limit)]


# ── Default ranker: substring + token overlap ────────────────────────


def _substring_rank(query: str, skills: list[Skill]) -> list[Skill]:
    """Score each skill by token overlap with query against when_to_use.

    Skills that don't match are filtered out entirely (vs the relevance_
    selector pattern which always returns ≤ limit). This means an
    irrelevant query that touches no skills returns an empty list,
    which is correct behaviour — the agent should run with NO skills
    surfaced rather than a random handful.
    """
    if not query.strip() or not skills:
        return []
    query_tokens = {t for t in query.lower().split() if len(t) >= 3}
    if not query_tokens:
        return []
    scored: list[tuple[int, Skill]] = []
    for skill in skills:
        haystack = (skill.when_to_use + " " + skill.description).lower()
        if not haystack.strip():
            continue
        overlap = sum(1 for t in query_tokens if t in haystack)
        if overlap > 0:
            scored.append((overlap, skill))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored]


__all__ = ["RelevanceRanker", "SkillRegistry"]
