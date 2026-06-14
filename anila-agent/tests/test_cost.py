"""token→貨幣換算：未知價格時 fail-soft（標 caveat、不編造 USD）。"""

from __future__ import annotations

import pytest

from anila_agent.observability import cost as cost_mod
from anila_agent.observability.cost import estimate_cost

pytestmark = pytest.mark.unit


class _Usage:
    input_tokens = 1000
    output_tokens = 500
    total_tokens = 1500


def test_unknown_model_returns_none_usd_with_caveat():
    est = estimate_cost("gpt-oss-20b", _Usage())
    assert est.usd is None
    assert est.total_tokens == 1500
    assert "未知" in est.note
    assert "1500 tokens" in est.render()


def test_known_price_computes_usd(monkeypatch):
    monkeypatch.setitem(cost_mod.PRICE_TABLE, "m", {"input": 1.0, "output": 2.0})
    est = estimate_cost("m", _Usage())
    # 1000/1e6*1 + 500/1e6*2 = 0.001 + 0.001 = 0.002
    assert est.usd == pytest.approx(0.002)
    assert "$" in est.render()


def test_missing_usage_fields_default_zero():
    est = estimate_cost("x", object())
    assert est.input_tokens == 0 and est.output_tokens == 0 and est.total_tokens == 0
