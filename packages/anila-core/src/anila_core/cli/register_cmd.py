"""anila-core register — register an agent on the ANILA CSP platform.

Reads anila.yaml from the current directory, authenticates with CSP
using a JWT login, and calls POST /api/agents/register.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml


_ANILA_YAML = "anila.yaml"

# doc-05 §3 runtime_type 的 5 個合法值。
_RUNTIME_TYPES: tuple[str, ...] = (
    "anila_agent",
    "langchain",
    "openwebui_pipe_compatible",
    "openai_compatible_agent",
    "custom_http",
)

# SYSTEM-MAP §8 四級分類（繁中值，default_classification_level 用）。
# 以前這裡叫 ``classification_ceiling``，但 agent 端沒有 ceiling 這回事：
# CSP 明確退回 agent 的 classification_ceiling，會寫入並被 enforce 的是
# ``default_classification_level``。旗標跟著改名，不再送一個註定被丟掉的欄位。
_CLASSIFICATION_LEVELS: tuple[str, ...] = (
    "無機密",
    "營業秘密",
    "密",
    "機密",
)


def _validate_choice(value: str, allowed: tuple[str, ...], flag: str) -> str:
    """Validate a flag value against a closed set; exit 1 with a clear message."""
    if value not in allowed:
        allowed_str = " / ".join(allowed)
        print(
            f"錯誤:{flag} 的值「{value}」不合法。可用的值:{allowed_str}",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def run(args: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="anila-core register",
        description="Register this agent on the ANILA CSP platform.",
    )
    parser.add_argument(
        "--csp", metavar="URL",
        default="",
        help="CSP base URL (e.g. http://localhost:8000). Overrides anila.yaml / env.",
    )
    parser.add_argument(
        "--endpoint", metavar="URL",
        default="",
        help="Agent endpoint URL. Overrides anila.yaml endpoint_url.",
    )
    parser.add_argument(
        "--username", "-u",
        default="",
        help="CSP username (developer or admin account). Prompted if omitted.",
    )
    parser.add_argument(
        "--manifest", metavar="PATH",
        default=_ANILA_YAML,
        help=f"Path to agent manifest file (default: {_ANILA_YAML}).",
    )
    parser.add_argument(
        "--runtime-type", metavar="TYPE",
        default="",
        help="Agent runtime type. One of: " + " / ".join(_RUNTIME_TYPES)
             + ". Overrides anila.yaml runtime_type.",
    )
    parser.add_argument(
        "--classification-level", metavar="LEVEL",
        default="",
        help="Default classification level for this agent's work. One of: "
             + " / ".join(_CLASSIFICATION_LEVELS)
             + ". Overrides anila.yaml default_classification_level.",
    )
    parser.add_argument(
        "--base-model", metavar="NAME",
        default="",
        help="Base model NAME this agent wraps (model_registry name or the "
             "display name shown in the governance console). "
             "Overrides anila.yaml base_model.",
    )
    parser.add_argument(
        "--base-model-id", metavar="ID",
        default="",
        help="Base model numeric id. Only needed when two models share a "
             "display name. Overrides anila.yaml base_model_id.",
    )
    parser.add_argument(
        "--version", metavar="VER",
        default="",
        help="Agent version string (e.g. 1.0.0). Overrides anila.yaml version.",
    )
    parsed = parser.parse_args(args)

    manifest = _load_manifest(parsed.manifest)

    # New optional metadata (Slice 5c). Flag overrides manifest; both validated
    # against their closed value sets before the payload is built.
    runtime_type = parsed.runtime_type or manifest.get("runtime_type", "")
    if runtime_type:
        manifest["runtime_type"] = _validate_choice(
            runtime_type, _RUNTIME_TYPES, "--runtime-type"
        )
    # 舊 manifest 用 ``classification_ceiling``。CSP 會 422 這個欄位（agent 端
    # 沒有 ceiling），所以明講要改名，而不是安靜丟掉開發者填的等級。
    if manifest.get("classification_ceiling"):
        print(
            "錯誤:anila.yaml 的 classification_ceiling 已退場(agent 端沒有分類上限)。"
            "請改名為 default_classification_level,值不變。",
            file=sys.stderr,
        )
        sys.exit(1)
    level = parsed.classification_level or manifest.get(
        "default_classification_level", ""
    )
    if level:
        manifest["default_classification_level"] = _validate_choice(
            level, _CLASSIFICATION_LEVELS, "--classification-level"
        )
    version = parsed.version or manifest.get("version", "")
    if version:
        manifest["version"] = version

    # 底層模型:名稱或 id 擇一。CSP 兩種都收（名稱由伺服器解析成 id），
    # 所以開發者不必先到治理中心翻一個數字 id 出來。
    base_model = parsed.base_model or manifest.get("base_model", "")
    if base_model:
        manifest["base_model"] = base_model
    base_model_id = parsed.base_model_id or manifest.get("base_model_id", "")
    if base_model_id not in ("", None):
        try:
            manifest["base_model_id"] = int(base_model_id)
        except (TypeError, ValueError):
            print(
                f"錯誤:base_model_id 必須是數字,收到「{base_model_id}」。"
                "如果你要用模型名稱,請改填 base_model。",
                file=sys.stderr,
            )
            sys.exit(1)
    if not manifest.get("base_model") and manifest.get("base_model_id") is None:
        print(
            "錯誤:必須指定底層模型。請在 anila.yaml 填 base_model:「模型名稱」"
            "(名稱可在治理中心「模型」頁查到),或用 --base-model 指定;"
            "若要用數字 id 則填 base_model_id / --base-model-id。",
            file=sys.stderr,
        )
        sys.exit(1)

    csp_url = (parsed.csp or _env("CSP_BASE_URL") or "http://localhost:8000").rstrip("/")
    if not parsed.csp and not _env("CSP_BASE_URL"):
        entered = input(f"CSP URL [{csp_url}]: ").strip()
        if entered:
            csp_url = entered.rstrip("/")

    endpoint_url = parsed.endpoint or manifest.get("endpoint_url", "")
    if not endpoint_url:
        endpoint_url = input("Agent endpoint URL (例:http://your-host:9100): ").strip()
        if not endpoint_url:
            print("錯誤:必須提供 endpoint_url", file=sys.stderr)
            sys.exit(1)
    manifest["endpoint_url"] = endpoint_url

    username = parsed.username or input("CSP 帳號: ").strip()
    if not username:
        print("錯誤:必須提供 CSP 帳號", file=sys.stderr)
        sys.exit(1)
    password = getpass.getpass(f"{username} 的密碼: ")

    jwt_token = _login(csp_url, username, password)
    result = _register(csp_url, jwt_token, manifest)

    print("\n✓ agent 已註冊")
    print(f"  ID       : {result['id']}")
    print(f"  名稱     : {result['name']}")
    print(f"  審批狀態 : {result['approval_status']}")
    print("\n下一步:請管理員在治理中心把這個 agent 指派給使用者(指派即自動核准)。")


def _load_manifest(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        print(
            f"錯誤:找不到「{path}」。請先執行 `anila-core init`,或用 --manifest 指定路徑。",
            file=sys.stderr,
        )
        sys.exit(1)
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    _require(data, "name", path)
    _require(data, "description_for_router", path)
    return data


def _require(data: dict, key: str, source: str) -> None:
    if not data.get(key):
        print(f"錯誤:{source} 缺少必填欄位「{key}」", file=sys.stderr)
        sys.exit(1)


def _login(csp_url: str, username: str, password: str) -> str:
    try:
        resp = httpx.post(
            f"{csp_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except httpx.HTTPStatusError as e:
        detail = _extract_detail(e.response)
        print(f"錯誤:登入失敗 — {detail}", file=sys.stderr)
        sys.exit(1)
    except httpx.RequestError as e:
        print(f"錯誤:連不到 CSP({csp_url})— {e}", file=sys.stderr)
        sys.exit(1)


def _register(csp_url: str, token: str, manifest: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "name": manifest["name"],
        "endpoint_url": manifest["endpoint_url"],
        "description_for_router": manifest["description_for_router"],
        "api_version": manifest.get("api_version", "v1"),
    }
    # 底層模型:名稱或 id。CSP 解析名稱 → id;兩個都送時必須指向同一個模型。
    if manifest.get("base_model"):
        payload["base_model_name"] = manifest["base_model"]
    if manifest.get("base_model_id") is not None:
        payload["base_model_id"] = manifest["base_model_id"]
    if manifest.get("input_schema"):
        payload["input_schema"] = manifest["input_schema"]
    # ⚠ CSP 的 AgentRegisterRequest 已是 extra="forbid":未宣告的欄位一律 422,
    # 不會再「收下然後丟掉」。這裡每一個 key 都必須對得上伺服器端的欄位
    # (registration.py 的 AgentRegisterRequest),新增欄位前先確認兩端一致。
    if manifest.get("runtime_type"):
        payload["runtime_type"] = manifest["runtime_type"]
    if manifest.get("version"):
        payload["version"] = manifest["version"]
    if manifest.get("default_classification_level"):
        payload["default_classification_level"] = manifest[
            "default_classification_level"
        ]

    try:
        resp = httpx.post(
            f"{csp_url}/api/agents/register",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        detail = _extract_detail(e.response)
        print(f"錯誤:註冊失敗 — {detail}", file=sys.stderr)
        sys.exit(1)
    except httpx.RequestError as e:
        print(f"錯誤:連不到 CSP({csp_url})— {e}", file=sys.stderr)
        sys.exit(1)


def _extract_detail(response: httpx.Response) -> str:
    """Render CSP's error body as one readable line.

    FastAPI's 422 ``detail`` is a *list* of validation errors; printing the
    raw list is how a developer ends up staring at ``[{'type': 'missing',
    'loc': ['body', 'base_model_id'], ...}]``. Flatten it to ``欄位: 訊息``.
    """
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        return response.text
    if isinstance(detail, list):
        lines = []
        for item in detail:
            if not isinstance(item, dict):
                lines.append(str(item))
                continue
            loc = ".".join(str(p) for p in item.get("loc", []) if p != "body")
            msg = item.get("msg", "")
            lines.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(lines)
    return detail if isinstance(detail, str) else str(detail)


def _env(key: str) -> str:
    import os
    return os.environ.get(key, "")
