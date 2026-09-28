"""把一筆 Studio 工作換成 CSP 簽發的工作委託權杖。

預檢仍用建立請求上的使用者權杖（同步 403/404）。runner 一開始就換權杖，
之後這筆工作打回 CSP 的呼叫，包含收尾回報，都用這張較長壽命的權杖。
換不到就讓工作失敗，不要退回快過期的使用者權杖。
"""

from __future__ import annotations

from typing import Any


async def adopt_job_bearer(
    user_bearer: str,
    job_id: str,
    collection_id: int,
    report_ctx: Any,
) -> str:
    """向 CSP 換權杖，寫進回報上下文，並回傳 runner 該用的 bearer。"""
    from app.clients.csp_client import mint_studio_job_token

    token = await mint_studio_job_token(
        user_bearer, job_id=job_id, collection_id=collection_id,
    )
    if report_ctx is not None:
        report_ctx.bearer = token
    return token
