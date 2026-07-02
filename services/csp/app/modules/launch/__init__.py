# -*- coding: utf-8 -*-
"""app.modules.launch — Service Launch Gateway(服務啟動閘道)。

職掌(doc 02 §1、doc 07、doc 10 §12):所有正式 GUI service launch 必須
通過本閘道——Service Registry 查核、Launch Contract / Launch Token 簽發,
與 launch 事件的 trace 紀錄。

邊界規則:其他程式碼只能 `from app.modules import launch` 或
`from app.modules.launch import ...`(package 根的公開介面),不得 import
本 package 子模組的內部實作。本 package 不得 import
`app.modules.tasks` / `app.modules.policy` 內部,也不得 import `app.api`。
"""

from app.modules.launch.manifest import (
    ManifestFetchError,
    fetch_service_manifest,
    manifest_url_for,
)
from app.modules.launch.service import (
    build_launch_url,
    create_service_launch,
    new_launch_id,
    new_trace_id,
    record_service_audit_callback,
)
from app.modules.launch.token import (
    LAUNCH_TOKEN_ISSUER,
    LAUNCH_TOKEN_TTL_MINUTES,
    build_launch_claims,
    issue_launch_token,
)

__all__ = [
    "LAUNCH_TOKEN_ISSUER",
    "LAUNCH_TOKEN_TTL_MINUTES",
    "ManifestFetchError",
    "build_launch_claims",
    "build_launch_url",
    "create_service_launch",
    "fetch_service_manifest",
    "issue_launch_token",
    "manifest_url_for",
    "new_launch_id",
    "new_trace_id",
    "record_service_audit_callback",
]
