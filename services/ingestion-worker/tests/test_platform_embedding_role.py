"""嵌入模型名稱只來自治理中心的平台嵌入角色，不猜環境變數裡的名字。"""
from __future__ import annotations

import pytest

from ingestion_worker.platform_embedding import (
    EmbeddingRoleUnset,
    resolve_from_pool,
)


class _Conn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *_args, **_kwargs):
        return self._row


class _Acquire:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return _Conn(self._row)

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, row):
        self._row = row

    def acquire(self):
        return _Acquire(self._row)


@pytest.mark.asyncio
async def test_unset_role_does_not_fall_back_to_a_settings_name():
    """沒有平台嵌入角色時，不得改用 EMBEDDING_MODEL / nvidia 預設名。"""
    with pytest.raises(EmbeddingRoleUnset):
        await resolve_from_pool(
            _Pool(None),
            settings_fallback_name="nvidia/NV-embed-V2",
        )


@pytest.mark.asyncio
async def test_designated_role_is_the_model_name():
    resolved = await resolve_from_pool(
        _Pool({"name": "console-embed", "embedding_native_dim": 4096}),
        settings_fallback_name="nvidia/NV-embed-V2",
    )
    assert resolved.name == "console-embed"
    assert resolved.native_dim == 4096
