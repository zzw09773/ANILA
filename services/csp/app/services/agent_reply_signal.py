"""Structural observation for unusually short registered-agent replies.

This module deliberately does not inspect reply wording.  A short reply can be
an upstream guard response or a model refusal, so the only supported claim is
the length observed by CSP.
"""

from __future__ import annotations

import logging
from typing import Literal


logger = logging.getLogger(__name__)

AGENT_REPLY_OBSERVATION_KEY = "agent_reply_observation"
AGENT_REPLY_SHORT_FLAG = "short_reply"
UsageSource = Literal["reported", "estimated"]

# Calibration recorded in docs/HANDOFF-2026-08-11.md §八: interception 121;
# model refusals 129 and 138; normal replies 219 and 365. 160 is conservative
# for the observed gap. The normal-sample n=2 is too small; this value awaits
# convergence against the real reply distribution.
AGENT_SHORT_REPLY_TOKENS = 160

_USAGE_SOURCES = frozenset({"reported", "estimated"})


def build_agent_reply_observation(
    completion_tokens: int,
    usage_source: UsageSource,
) -> dict[str, int | str | bool] | None:
    """Return the platform-only observation, or ``None`` on internal failure.

    The helper is intentionally pure: it does not read settings, perform I/O,
    inspect text, or raise into the response path.
    """
    try:
        if isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int):
            raise TypeError("completion_tokens must be an integer")
        if completion_tokens < 0:
            raise ValueError("completion_tokens must be non-negative")
        if usage_source not in _USAGE_SOURCES:
            raise ValueError("usage_source must be reported or estimated")

        observation: dict[str, int | str | bool] = {
            "completion_tokens": completion_tokens,
            "usage_source": usage_source,
        }
        if completion_tokens <= AGENT_SHORT_REPLY_TOKENS:
            observation[AGENT_REPLY_SHORT_FLAG] = True
        return observation
    except Exception:  # pragma: no cover - defensive serving boundary
        logger.exception("agent reply observation construction failed")
        return None


def attach_agent_reply_observation(
    metadata: object,
    *,
    completion_tokens: int,
    usage_source: UsageSource,
) -> object:
    """Replace the agent observation in a mutable meta dict, never raising."""
    try:
        if not isinstance(metadata, dict):
            raise TypeError("agent meta must be a dict")
        # The platform owns this observation; do not trust a downstream copy.
        metadata.pop(AGENT_REPLY_OBSERVATION_KEY, None)
        observation = build_agent_reply_observation(
            completion_tokens,
            usage_source,
        )
        if observation is not None:
            metadata[AGENT_REPLY_OBSERVATION_KEY] = observation
    except Exception:  # pragma: no cover - defensive serving boundary
        logger.exception("agent reply observation attachment failed")
    return metadata
