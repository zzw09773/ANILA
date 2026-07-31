"""The governance UI's register payload must match the endpoint's contract.

``AgentRegisterRequest`` is ``extra="forbid"``. That is the right trade —
it is what stops a client/server drift being silently swallowed — but it
moves the cost of a drift from "field quietly dropped" to "every
registration in the institute 422s". The UI form is a plain reactive
object that anyone may add a purely-cosmetic key to (a collapse flag, a
draft toggle), so the payload must be built from an explicit field list,
never ``...form.value``.

Why this lives in the csp suite rather than beside the UI:
``apps/csp-governance-ui/tests/*.test.mjs`` (``node --test``) already
follows exactly this read-the-source pattern and would be the idiomatic
home for the "no spread" half — but the other half has to compare the
payload against ``AgentRegisterRequest.model_fields``, which only Python
can see. Keeping both halves together also puts them in the suite the
repo actually declares (README.md: ``cd services/csp && pytest``); the
node tests have no npm script and no CI wiring.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.api.agents.registration import AgentRegisterRequest, _repo_root

VIEW = _repo_root() / "apps/csp-governance-ui/src/views/DeveloperAgentsView.vue"

pytestmark = pytest.mark.skipif(
    not VIEW.is_file(), reason="governance UI sources not present in this checkout"
)


def _register_payload_literal() -> str:
    """The object literal passed to ``registerAgent({...})``, brace-matched."""
    src = VIEW.read_text(encoding="utf-8")
    start = src.index("registerAgent({") + len("registerAgent(")
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError("unbalanced braces in the registerAgent(...) call")


def _top_level_keys(literal: str) -> set[str]:
    """Keys written at depth 1 of the literal (ignores nested objects)."""
    keys: set[str] = set()
    depth = 0
    for line in literal.splitlines():
        stripped = line.strip()
        if depth == 1:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", stripped)
            if m:
                keys.add(m.group(1))
        depth += line.count("{") + line.count("[")
        depth -= line.count("}") + line.count("]")
    return keys


def test_payload_is_not_built_by_spreading_the_form():
    """`...form.value` would make any new UI-only form key 422 every register."""
    literal = _register_payload_literal()
    offenders = [
        ln.strip() for ln in literal.splitlines()
        if re.match(r"^\.\.\.", ln.strip())
    ]
    assert not offenders, (
        "register payload spreads an object instead of listing fields: "
        f"{offenders}. AgentRegisterRequest is extra=\"forbid\" — a spread "
        "turns any new form-only key into a 422 for every registration."
    )


def test_every_key_the_ui_sends_is_declared_by_the_endpoint():
    declared = set(AgentRegisterRequest.model_fields)
    for field in AgentRegisterRequest.model_fields.values():
        alias = getattr(field, "validation_alias", None)
        for choice in getattr(alias, "choices", []) or []:
            if isinstance(choice, str):
                declared.add(choice)

    sent = _top_level_keys(_register_payload_literal())
    assert sent, "could not parse any keys out of the register payload"
    undeclared = sent - declared
    assert not undeclared, (
        f"governance UI sends undeclared field(s) {sorted(undeclared)}; "
        "with extra=\"forbid\" these 422 every registration. Declare them in "
        "AgentRegisterRequest or stop sending them."
    )


def test_the_required_fields_are_actually_sent():
    """A guard against 'fixed the spread, dropped a field' — id came from it."""
    sent = _top_level_keys(_register_payload_literal())
    for required in ("name", "endpoint_url", "description_for_router",
                     "api_version", "base_model_id"):
        assert required in sent, f"register payload no longer sends {required}"
