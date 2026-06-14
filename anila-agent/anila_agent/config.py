"""設定載入：環境變數為主、``configs/*.yaml`` 為輔，env 優先。

保留平台 env 契約與 fallback 鏈。設定物件為 frozen dataclass（immutable）。
P0 僅涵蓋模型與核心欄位；retrieval（PGVECTOR_* / ANILA_CSP_*）於 P1 加入。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# model.yaml 的 settings 只套用這些已知 key；未知 key 靜默丟棄（平台契約行為）。
_SETTINGS_ALLOWLIST = frozenset(
    {"temperature", "top_p", "max_tokens", "tool_choice", "parallel_tool_calls",
     "frequency_penalty", "presence_penalty"}
)
_FALSEY = frozenset({"0", "false", "no", "off", ""})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def _filter_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """只留 allowlist 內的 key（未知 key 不報錯，靜默丟棄）。"""
    return {k: v for k, v in raw.items() if k in _SETTINGS_ALLOWLIST and v is not None}


@dataclass(frozen=True)
class ModelConfig:
    """指向本地 OpenAI-compatible 端點（vLLM）的模型設定。"""

    base_url: str
    model: str
    api_key: str
    ssl_verify: bool
    timeout: float
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentConfig:
    name: str
    max_turns: int


@dataclass(frozen=True)
class AppConfig:
    model: ModelConfig
    agent: AgentConfig
    home: Path
    log_level: str


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    import yaml  # 延遲匯入：未裝 yaml 時不影響純 env 路徑

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} 頂層必須是 mapping，實得 {type(data).__name__}")
    return data


def load_config(config_dir: str | os.PathLike[str] | None = None) -> AppConfig:
    """組裝 AppConfig。yaml 提供預設，環境變數覆寫之。"""
    cfg_dir = Path(config_dir) if config_dir is not None else Path("configs")
    model_yaml = _load_yaml(cfg_dir / "model.yaml")

    # base_url / model：env 優先，否則 yaml，否則內建預設（內網現況：gpt-oss-20b）。
    base_url = os.getenv("ANILA_BASE_URL") or model_yaml.get("base_url") or "http://localhost:8000/v1"
    model = os.getenv("ANILA_MODEL") or model_yaml.get("model") or "gpt-oss-20b"

    # api key：yaml 可指定 api_key_env 改讀別的 env 名；預設 ANILA_API_KEY；缺省 "EMPTY"。
    api_key_env = model_yaml.get("api_key_env", "ANILA_API_KEY")
    api_key = os.getenv(api_key_env) or "EMPTY"

    settings = _filter_settings(model_yaml.get("settings", {}))

    model_cfg = ModelConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        ssl_verify=_env_bool("ANILA_SSL_VERIFY", True),
        timeout=float(os.getenv("ANILA_TIMEOUT", "60")),
        settings=settings,
    )

    agent_yaml = _load_yaml(cfg_dir / "agent.yaml")
    agent_cfg = AgentConfig(
        name=os.getenv("ANILA_AGENT_NAME") or agent_yaml.get("name") or "ANILA Agent",
        max_turns=int(os.getenv("ANILA_MAX_TURNS") or agent_yaml.get("max_turns") or 10),
    )

    home = Path(os.getenv("ANILA_HOME") or ".anila").expanduser()
    log_level = (os.getenv("ANILA_LOG_LEVEL") or "INFO").upper()

    return AppConfig(model=model_cfg, agent=agent_cfg, home=home, log_level=log_level)


def with_model(cfg: AppConfig, **overrides: Any) -> AppConfig:
    """回傳套用模型覆寫後的新 AppConfig（immutable 更新，不改原物件）。"""
    return replace(cfg, model=replace(cfg.model, **overrides))
