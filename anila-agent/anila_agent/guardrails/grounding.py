"""接地 output guardrail：CitedAnswer 有實質內容卻無引用時 tripwire。

opt-in（隨 ANILA_CITED=1 與 CitedAnswer output_type 一起啟用）。強制模型的回答附來源，
否則 SDK 觸發 OutputGuardrailTripwireTriggered。
"""

from __future__ import annotations

from typing import Any

from agents import GuardrailFunctionOutput, output_guardrail

from anila_agent.models.schemas import CitedAnswer

# 短於此長度的回答（如「查無相關內容」）豁免引用要求。CJK 密度高，門檻取較低值。
_MIN_LEN_FOR_CITATION = 20


@output_guardrail(name="grounding")
async def grounding_guardrail(
    context: Any, agent: Any, agent_output: Any
) -> GuardrailFunctionOutput:
    """回答有實質內容卻無 citation → tripwire。"""
    tripped = (
        isinstance(agent_output, CitedAnswer)
        and len(agent_output.answer.strip()) >= _MIN_LEN_FOR_CITATION
        and not agent_output.citations
    )
    return GuardrailFunctionOutput(
        output_info={"cited": bool(getattr(agent_output, "citations", None))},
        tripwire_triggered=bool(tripped),
    )
