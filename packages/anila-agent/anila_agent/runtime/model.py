"""把 OpenAI Agents SDK 指向本地 vLLM 端點，並鎖死 air-gap 不變式。

三條鐵律（``_lock_airgap_invariants``，程序啟動鎖一次）：
  1. 強制 Chat Completions —— vLLM 不實作 SDK 預設的 Responses API。
  2. 關閉預設 tracing exporter —— 否則會 POST 到 platform.openai.com（air-gap 洩漏/401）。
  3. 建構 ``OpenAIChatCompletionsModel`` 物件（非字串模型名）並配明確 ``ModelSettings``，
     避免 SDK 對字串模型名自動套用 GPT-5 reasoning 預設、洩漏到本地模型。

另兩項防護：
  - ``ANILA_SSL_VERIFY`` 接進 httpx client（自簽內網 vLLM 的 chat 路徑否則直接失敗）。
  - reasoning 模型 ``content=None`` until done：``max_tokens`` 給 >= ``REASONING_MAX_TOKENS_FLOOR``
    下限（gpt-oss/gemma4 皆為 reasoning 模型）。結構化輸出的解析端防護見 ``util.structured``。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from agents import (
    ModelSettings,
    OpenAIChatCompletionsModel,
    set_default_openai_api,
    set_tracing_disabled,
)
from openai import AsyncOpenAI

from anila_agent.config import ModelConfig
from anila_agent.runtime.admission import assert_csp_model_endpoint
from anila_agent.runtime.compat import build_http_client

# reasoning 模型在吐出最終 content 前會先耗 token 推理；下限確保不被截斷成空。
REASONING_MAX_TOKENS_FLOOR = 512

_AIRGAP_LOCKED = False


def _lock_airgap_invariants() -> None:
    """設定全域 air-gap 不變式；冪等，只生效一次。"""
    global _AIRGAP_LOCKED
    if _AIRGAP_LOCKED:
        return
    set_default_openai_api("chat_completions")
    set_tracing_disabled(True)
    _AIRGAP_LOCKED = True


def build_model(
    cfg: ModelConfig,
    *,
    csp_base_url: str | None = None,
    require_csp_endpoint: bool = False,
) -> OpenAIChatCompletionsModel:
    """建構 Chat Completions 模型。

    Standalone library callers may still construct a local model for an
    explicitly offline smoke test.  The official service startup path passes
    ``require_csp_endpoint=True``; that path is the Silver admission boundary
    and rejects a raw model-service URL before an HTTP client is created.
    """
    if require_csp_endpoint:
        if not csp_base_url:
            raise ValueError("require_csp_endpoint=True 必須提供 csp_base_url")
        assert_csp_model_endpoint(cfg.base_url, csp_base_url)
    _lock_airgap_invariants()
    # 自簽 TLS（ssl_verify）+ 剝除 strict 欄位（自架端點相容）都在此 http client。
    http_client = build_http_client(verify=cfg.ssl_verify, timeout=cfg.timeout)
    # Official service mode reaches the model only through CSP.  The csk is
    # provisioned per Agent and must be carried in the CSP service-token
    # header; never synthesize an Agent identity from a client-supplied
    # ``X-ANILA-Agent-Id`` header.  ``service_wrapper`` validates the env
    # token's canonical shape at startup; this client simply reuses that
    # already-admitted secret.  Keep the local no-token development path
    # compatible by falling back to the configured SDK key.
    csp_service_token = (os.getenv("CSP_SERVICE_TOKEN") or "").strip()
    if not csp_service_token and isinstance(cfg.api_key, str) and cfg.api_key.startswith("csk-"):
        csp_service_token = cfg.api_key
    api_key = csp_service_token or cfg.api_key or "EMPTY"
    default_headers = (
        {"X-CSP-Service-Token": csp_service_token}
        if require_csp_endpoint and csp_service_token
        else None
    )
    client = AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=api_key,  # CSP authenticates the csk header; local vLLM accepts any non-empty key.
        http_client=http_client,
        default_headers=default_headers,
    )
    # keyword-only：第三位置參數是 should_replay_reasoning_content，未來欄位插入不可位移。
    return OpenAIChatCompletionsModel(model=cfg.model, openai_client=client)


def build_model_settings(cfg: ModelConfig) -> ModelSettings:
    """由 allowlist 過的 settings 組 ModelSettings，並對 max_tokens 套 reasoning 下限。"""
    s = dict(cfg.settings)
    max_tokens = max(int(s.get("max_tokens") or 0), REASONING_MAX_TOKENS_FLOOR)
    return ModelSettings(
        temperature=s.get("temperature"),
        top_p=s.get("top_p"),
        max_tokens=max_tokens,
        tool_choice=s.get("tool_choice"),
        parallel_tool_calls=s.get("parallel_tool_calls"),
    )


def json_object_settings(cfg: ModelConfig) -> ModelSettings:
    """要求 response_format=json_object 的 ModelSettings（結構化側查詢用）。

    實測：自架 reasoning 模型（gpt-oss/tensorrt-llm）對 SDK 的 output_type（json_schema
    guided decoding）不可靠，會回 markdown；改用 json_object + 防禦性解析才穩。
    """
    import dataclasses

    base = build_model_settings(cfg)
    extra: dict[str, object] = {}
    if isinstance(base.extra_body, Mapping):
        extra.update({str(key): value for key, value in base.extra_body.items()})
    extra["response_format"] = {"type": "json_object"}
    return dataclasses.replace(base, extra_body=extra)
