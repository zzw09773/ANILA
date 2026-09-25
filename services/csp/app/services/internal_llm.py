"""平台內部的 chat completion。

憑證、SSRF guard、逾時、上游路徑拼接與取樣都走 ``proxy_request``。
這裡不解密、不自己組 Authorization。

用量歸戶
========
記憶整理與思考進度是平台替使用者做的背景工作，使用者沒有要求這一輪計費。
這類呼叫記 ``request_type="internal"``、``usage_kind="platform"``。
``user_id`` 仍寫來源使用者，方便對到是哪一段對話觸發的；
``usage_service`` 加總時排除 ``platform``（與 ``router_transport`` 相同），
所以不進使用者帳單，也不灌推理總量。列留在 ``token_usage``。

開發者按「產生 system prompt」是使用者自己要的呼叫，
``on_behalf_of_user=True``，照一般 chat 記入該使用者。

沒有 ``user_id`` 時 ``token_usage.user_id`` 不能空，改為不記用量。

對話標題與手動壓縮目前由殼層／Router 打公開 ``/v1/chat/completions``，
已經走 proxy 注入模型金鑰，不是這裡的直接呼叫端。
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.orm import Session

_SNAPSHOT_FIELDS = (
    "id",
    "name",
    "display_name",
    "model_type",
    "endpoint_url",
    "api_version",
    "protocol",
    "api_key_secret_ref",
    "classification_ceiling",
    "is_active",
    "is_internal",
    "thinking_effort",
    "thinking_levels_supported",
    "thinking_user_selectable",
    "temperature",
    "top_p",
    "presence_penalty",
    "max_tokens",
)


class InternalCompletionError(Exception):
    """模型呼叫失敗或沒有可用內文。不帶上游網址。"""

    def __init__(self, status_code: int | None = None):
        self.status_code = status_code
        super().__init__("內部模型呼叫失敗")


def _snapshot(model) -> SimpleNamespace:
    data = {name: getattr(model, name, None) for name in _SNAPSHOT_FIELDS}
    if data.get("api_version") not in ("v1", "v2"):
        data["api_version"] = "v1"
    if not data.get("protocol"):
        data["protocol"] = "openai_compatible"
    if not data.get("model_type"):
        data["model_type"] = "llm"
    if data.get("id") is None:
        data["id"] = 0
    return SimpleNamespace(**data)


def _assistant_text(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        return ""
    content = message.get("content") or ""
    return content if isinstance(content, str) else ""


async def complete_chat(
    db: Session,
    model,
    body: dict,
    *,
    user_id: int | None,
    department_id: int | None = None,
    on_behalf_of_user: bool = False,
) -> str:
    """對一列模型做一次非串流 chat completion，回傳助理內文。

    呼叫端要在進這裡之前讀完資料庫。這個函式會 ``commit`` 把連線還回池子，
    再走出向 HTTP。失敗丟 :class:`InternalCompletionError`。
    """
    from app.services.proxy.service import proxy_request, resolve_proxy_tuning

    snapshot = _snapshot(model)
    tuning = resolve_proxy_tuning(db)
    # 逾時在還握著連線時解析；commit 之後不再查 platform_settings。
    db.commit()
    payload = dict(body)
    payload["model"] = snapshot.name
    record = user_id is not None
    try:
        result = await proxy_request(
            model=snapshot,
            api_key_id=None,
            user_id=user_id if user_id is not None else 0,
            department_id=department_id,
            request_body=payload,
            endpoint_path=f"/{snapshot.api_version}/chat/completions",
            record_usage=record,
            usage_kind="inference" if on_behalf_of_user else "platform",
            request_type_override=None if on_behalf_of_user else "internal",
            tuning=tuning,
        )
    except HTTPException as exc:
        raise InternalCompletionError(exc.status_code) from exc
    text = _assistant_text(result).strip()
    if not text:
        raise InternalCompletionError(None)
    return text
