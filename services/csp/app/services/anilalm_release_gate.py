# -*- coding: utf-8 -*-
"""ANILA LM 發行閘門 —— 後端(API payload)這一面。

2026-09-26 起開放。同一個閘門前端還有兩份旗標:

  * ``apps/anila-shell/src/anilalmReleaseGate.js``       (shell 左側導覽)
  * ``apps/csp-governance-ui/src/utils/anilalmReleaseGate.js`` (治理中心儀表板)

再加上 ``infra/nginx/anila.conf`` 的 ``/anilalm`` 代理(關上時改回
``~* ^/anilalm`` 503)。那三道擋的都是「瀏覽得到的門」,擋不到 **API 回應
本身把 ANILA LM 列成一個可用服務** —— shell 的「專案入口」抽屜就是照
``GET /api/services`` 畫的,而任何人只要知道 service id 就能打
``POST /api/services/{id}/launch``。這份就是補那一面,不是第二套機制:
旗標語意、命名、比對規則都與前端兩份對齊。

**閘門關的是「可用」,不是「可管理」。**
管理面(``include_inactive=true`` 的管理清單、單筆 CRUD、授權管理)一律
照常回傳 ANILA LM。把列從管理員眼前拿掉,他就沒辦法停用/編輯/刪除它 ——
那不是 release gate,那是把管理員鎖在門外(2026-08-02 踩過:治理中心的
「平台連結」在前端濾掉整列,連編輯/停用/刪除按鈕一起消失,要停用只能手打
API)。

2026-09-26 起 ``ANILA_LM_RELEASED`` 為 True。要再關上:改回 False,並依
``docs/runbooks/anilalm-release-gate.md`` 一併關上 nginx / shell / 治理中心。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 與前端 ANILA_LM_ENTRY_ENABLED / ANILA_LM_LINK_VISIBLE 同語意的第四個開關。
# 2026-09-26 擁有者決定開放。
ANILA_LM_RELEASED = True

# 與 anilalmReleaseGate.js 的 isAnilaLmPlatformLink 對齊的比對規則。
_NAME_MATCHES = frozenset({"anila lm", "anilalm"})
_SLUG_MATCHES = frozenset({"anila-lm", "anilalm"})
# /anilalm、/anilalm/、含 query 的深連;大小寫不敏感(nginx 那道也是 ``~*``)。
_URL_RE = re.compile(r"(?:^|/)anilalm(?:/|$|\?)", re.IGNORECASE)


def is_anila_lm_service(service: Any) -> bool:
    """這一筆註冊服務是不是 ANILA LM 入口。

    名稱 / slug / env_seed_key / entry_url 任一命中即算 —— 種子改名或管理員
    改 slug 都不該讓閘門漏掉(fail closed 的方向是「多擋」而非「漏放」)。
    """
    if service is None:
        return False
    name = str(getattr(service, "name", "") or "").strip().lower()
    if name in _NAME_MATCHES:
        return True
    slug = str(getattr(service, "slug", "") or "").strip().lower()
    if slug in _SLUG_MATCHES:
        return True
    seed_key = str(getattr(service, "env_seed_key", "") or "").strip().lower()
    if seed_key in _NAME_MATCHES:
        return True
    url = str(getattr(service, "entry_url", "") or "").strip()
    return bool(url) and bool(_URL_RE.search(url))


def is_gated(service: Any) -> bool:
    """True ⇒ 閘門關著且這筆就是 ANILA LM:不得以「可用服務」之姿對外呈現,
    也不得啟動。"""
    if ANILA_LM_RELEASED:
        return False
    return is_anila_lm_service(service)


def filter_available(services: Iterable[Any]) -> list:
    """使用者面的「可用服務」清單 —— 閘門關閉時濾掉 ANILA LM。

    ⚠ 只能用在使用者面(shell 專案入口、儀表板外部工具卡)。管理面不要套。
    """
    rows = list(services or [])
    if ANILA_LM_RELEASED:
        return rows
    return [row for row in rows if not is_anila_lm_service(row)]


# 使用者看到的拒絕字樣,與 nginx 503 維護頁同一句話(同一件事只講一種說法)。
GATED_LAUNCH_DETAIL = "ANILA LM 尚未開放,此功能仍在整備中"
