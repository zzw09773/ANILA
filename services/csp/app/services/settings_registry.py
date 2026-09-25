# -*- coding: utf-8 -*-
"""治理頁唯一的即時設定登錄表。

本輪設定收斂後，這裡只宣告真正能在請求期間被消費、而且改完下一個
請求就生效的十七顆 C 類設定。部署事實、秘密與程式常數不再假裝是
平台設定，也不再由治理頁承諾「重啟後會生效」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from anila_core.api import router_prompts as _router_prompts

from app.models.platform_setting import (
    KB_THRESHOLD_DEFAULT,
    KB_THRESHOLD_KEY,
    _is_usable_kb_threshold,
)


class UnknownSettingError(KeyError):
    """登錄表沒有這個 key。"""


class SettingClass(Enum):
    """目前唯一允許進入治理頁的設定類別。"""

    C = "C"


EDITABLE_CLASSES = frozenset({SettingClass.C})


@dataclass(frozen=True)
class SettingType:
    """設定值的解析與正規化規則。"""

    name: str
    py_type: type
    parse: Callable[[str], Any]
    format: Callable[[Any], str]


def _parse_int(raw: str) -> int:
    return int(raw.strip())


def _parse_float(raw: str) -> float:
    return float(raw.strip())


def _parse_flag_ne_0(raw: str) -> bool:
    """intl 兩顆開關的既有消費契約：只有字串 ``0`` 代表關閉。"""

    return raw != "0"


def _format_int(value: int) -> str:
    return str(int(value))


def _format_float(value: float) -> str:
    return repr(float(value))


def _format_flag(value: bool) -> str:
    return "1" if value else "0"


T_INT = SettingType("int", int, _parse_int, _format_int)
T_FLOAT = SettingType("float", float, _parse_float, _format_float)
T_BOOL_NE_0 = SettingType("bool(!= '0')", bool, _parse_flag_ne_0, _format_flag)
# 多行文字（system prompt）。存取都是原樣字串；值域由各顆的 domain_fn 把關。
T_TEXT = SettingType("text", str, str, str)


def _non_blank_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _formattable_system_prompt(value: Any) -> bool:
    """Router 派工模板每次請求都 ``.format(agent_list=…)``：沒有佔位或多了別的
    大括號，router 端會逐請求炸掉。寫入端就擋。"""
    return _non_blank_text(value) and _router_prompts.system_template_is_formattable(value)


def _closed_int_range(low: int, high: int) -> Callable[[Any], bool]:
    def _in_range(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high

    _in_range.__doc__ = f"整數，閉區間 [{low}, {high}]。"
    _in_range.bounds = (low, high)
    return _in_range


def _closed_float_range(low: float, high: float) -> Callable[[Any], bool]:
    def _in_range(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and (
            low <= float(value) <= high
        )

    _in_range.__doc__ = f"數值，閉區間 [{low}, {high}]。"
    _in_range.bounds = (low, high)
    return _in_range


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


@dataclass(frozen=True)
class SettingSpec:
    """一顆 C 類設定的完整宣告。"""

    key: str
    env_name: str | None
    setting_class: SettingClass
    value_type: SettingType
    domain_fn: Callable[[Any], bool]
    default: Any
    description: str


def _spec(
    key: str,
    env_name: str | None,
    value_type: SettingType,
    domain_fn: Callable[[Any], bool],
    default: Any,
    description: str,
) -> SettingSpec:
    return SettingSpec(
        key=key,
        env_name=env_name,
        setting_class=SettingClass.C,
        value_type=value_type,
        domain_fn=domain_fn,
        default=default,
        description=description,
    )


SETTINGS: tuple[SettingSpec, ...] = (
    _spec(
        KB_THRESHOLD_KEY,
        None,
        T_FLOAT,
        _is_usable_kb_threshold,
        KB_THRESHOLD_DEFAULT,
        "院內規章檢索的分數門檻。允許 0–1（兩端都含）。",
    ),
    _spec(
        "memory.retrieve_min_cosine",
        "MEMORY_RETRIEVE_MIN_COSINE",
        T_FLOAT,
        _closed_float_range(0.0, 1.0),
        0.4,
        "對話摘要搜尋的相似度門檻。允許 0–1（兩端都含）。",
    ),
    _spec(
        "memory.retrieve_top_k",
        "MEMORY_RETRIEVE_TOP_K",
        T_INT,
        _closed_int_range(1, 100),
        3,
        "對話摘要搜尋取回的筆數。允許 1–100 筆。",
    ),
    _spec(
        "memory.enabled",
        "MEMORY_ENABLED",
        T_BOOL_NE_0,
        _is_bool,
        True,
        "長期記憶總開關。關閉後不再注入事實、不再萃取，也不搜尋對話摘要。",
    ),
    _spec(
        "memory.idle_minutes",
        "MEMORY_IDLE_MINUTES",
        T_INT,
        _closed_int_range(1, 1440),
        10,
        "對話閒置幾分鐘後才萃取摘要與事實。允許 1–1440 分鐘。",
    ),
    _spec(
        "proxy.llm_timeout",
        "LLM_TIMEOUT",
        T_INT,
        _closed_int_range(1, 3600),
        300,
        "LLM 呼叫逾時秒數。允許 1–3600 秒。思考型模型常超過 120 秒才開始輸出。",
    ),
    _spec(
        "proxy.embedding_timeout",
        "EMBEDDING_TIMEOUT",
        T_INT,
        _closed_int_range(1, 3600),
        30,
        "嵌入呼叫逾時秒數。允許 1–3600 秒。",
    ),
    _spec(
        "auth.access_token_expire_minutes",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        T_INT,
        _closed_int_range(1, 1440),
        60,
        "access token 有效分鐘數。允許 1–1440 分鐘；只影響下一次簽發。",
    ),
    _spec(
        "auth.refresh_token_expire_days",
        "REFRESH_TOKEN_EXPIRE_DAYS",
        T_INT,
        _closed_int_range(1, 365),
        30,
        "refresh token 有效天數。允許 1–365 天；只影響下一次簽發。",
    ),
    _spec(
        "limits.department_max_depth",
        "ANILA_DEPARTMENT_MAX_DEPTH",
        T_INT,
        _closed_int_range(1, 10),
        3,
        "部門樹最大層數。允許 1–10 層。",
    ),
    _spec(
        "limits.action_invoke_per_min",
        "ANILA_ACTION_INVOKE_PER_MIN",
        T_INT,
        _closed_int_range(1, 10000),
        20,
        "每使用者每分鐘的自訂動作呼叫上限。允許 1–10000 次。",
    ),
    _spec(
        "limits.attachment_budget_ratio",
        "ANILA_ATTACHMENT_BUDGET_RATIO",
        T_FLOAT,
        _closed_float_range(0.0, 1.0),
        0.7,
        "附件可佔用 context window 的比例。允許 0–1（兩端都含）。",
    ),
    _spec(
        "intl.zh_normalize",
        "ANILA_ZH_NORMALIZE",
        T_BOOL_NE_0,
        _is_bool,
        True,
        "把模型輸出的簡體字正規化成繁體。",
    ),
    _spec(
        "intl.query_expansion",
        "ANILA_QUERY_EXPANSION",
        T_BOOL_NE_0,
        _is_bool,
        True,
        "檢索前做同義詞查詢擴展。",
    ),
    _spec(
        _router_prompts.KEY_SYSTEM,
        None,
        T_TEXT,
        _formattable_system_prompt,
        _router_prompts.DEFAULT_ROUTER_SYSTEM,
        "Router 派工模板（有已註冊 agent 時的 system prompt）。誰改＝平台管理員；情境＝營運期調整回答口氣／派工準則；為何不能等改版＝prompt 調優是高頻營運動作（擁有者 2026-08-22 裁定）。儲存後 router 在 30 秒內生效（不需重建、不需 recreate）。「重設為出貨預設」＝把出貨全文存回去。必須保留 {agent_list} 佔位，且不得有其他大括號。",
    ),
    _spec(
        _router_prompts.KEY_PLAIN,
        None,
        T_TEXT,
        _non_blank_text,
        _router_prompts.DEFAULT_PLAIN_ASSISTANT,
        "Router 直答模板（沒有任何 agent 時的 system prompt）。誰改＝平台管理員；情境＝營運期調整回答口氣／派工準則；為何不能等改版＝prompt 調優是高頻營運動作（擁有者 2026-08-22 裁定）。儲存後 router 在 30 秒內生效（不需重建、不需 recreate）。「重設為出貨預設」＝把出貨全文存回去。",
    ),
    _spec(
        _router_prompts.KEY_FORCED,
        None,
        T_TEXT,
        _non_blank_text,
        _router_prompts.DEFAULT_FORCED_ANSWER,
        "Router 強制自答模板（使用者要求「你自己依院內規章回答」時的 system prompt）。誰改＝平台管理員；情境＝營運期調整回答口氣／派工準則；為何不能等改版＝prompt 調優是高頻營運動作（擁有者 2026-08-22 裁定）。儲存後 router 在 30 秒內生效（不需重建、不需 recreate）。「重設為出貨預設」＝把出貨全文存回去。",
    ),
)


REGISTRY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS}


def require_spec(key: str) -> SettingSpec:
    """取一筆宣告；未知 key 一律明確失敗。"""

    try:
        return REGISTRY[key]
    except KeyError:
        raise UnknownSettingError(f"未知的設定 key：{key!r}") from None
