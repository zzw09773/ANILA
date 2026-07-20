"""token → 貨幣的薄價格表層。

SDK 的 Usage 只算 token，從不算錢。本層提供可選的貨幣換算；自架模型
（gpt-oss-20b / gemma4 等）價格通常「未知」——此時**標註 caveat 並只顯示 token**，
而非編造 USD（避免操作者誤估配額）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 每百萬 token 美元；None = 未知。自架模型預設未知（沒有對外計價）。
# 操作者若有內部計價，可在此填入。
PRICE_TABLE: dict[str, dict[str, float | None]] = {
    # "gpt-oss-20b": {"input": 0.0, "output": 0.0},
}


@dataclass(frozen=True)
class CostEstimate:
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    usd: float | None
    note: str

    def render(self) -> str:
        if self.usd is None:
            return f"{self.model}: {self.total_tokens} tokens（{self.note}）"
        return f"{self.model}: {self.total_tokens} tokens ≈ ${self.usd:.4f}"


def estimate_cost(model: str, usage: Any) -> CostEstimate:
    """由 Usage 算費用；無價格表時 usd=None 並附 caveat。"""
    in_tok = int(getattr(usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(usage, "output_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or (in_tok + out_tok))
    price = PRICE_TABLE.get(model)
    input_price = price.get("input") if price is not None else None
    output_price = price.get("output") if price is not None else None
    if input_price is None or output_price is None:
        return CostEstimate(model, in_tok, out_tok, total, None, "價格未知（自架模型），僅計 token")
    usd = in_tok / 1_000_000 * input_price + out_tok / 1_000_000 * output_price
    return CostEstimate(model, in_tok, out_tok, total, usd, "")
