"""平台共同前導（system prompt 前綴）的單一事實來源。"""

from anila_core.prompts.common_preamble import (
    COMMON_PREAMBLE,
    DATA_DISCIPLINE,
    ERA_RULES,
    IDENTITY,
    LANGUAGE_PREAMBLE,
    LANGUAGE_RULES,
    NATIONAL_TERMINOLOGY,
    compose,
)
from anila_core.prompts.current_facts import (
    CURRENT_FACTS,
    FACTS_AS_OF,
    OFFICEHOLDERS,
)

__all__ = [
    "COMMON_PREAMBLE",
    "CURRENT_FACTS",
    "DATA_DISCIPLINE",
    "ERA_RULES",
    "FACTS_AS_OF",
    "IDENTITY",
    "LANGUAGE_PREAMBLE",
    "LANGUAGE_RULES",
    "NATIONAL_TERMINOLOGY",
    "OFFICEHOLDERS",
    "compose",
]
