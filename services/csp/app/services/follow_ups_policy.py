"""追問建議的固定能力與對外資料邊界；使用者偏好不能啟用。"""
from __future__ import annotations


def follow_ups_available() -> bool:
    """沒有核定的任意文字驗證能力時，停止生成及歷史建議回放。"""
    return False


def follow_ups_capability() -> dict:
    return {
        "available": follow_ups_available(),
        "reason": "FOLLOW_UPS_UNAVAILABLE",
        "message": "追問建議目前未提供",
    }


def public_message_metadata(metadata):
    """移除不可用的建議欄位，保留其他 metadata 且不修改儲存資料。"""
    if not isinstance(metadata, dict) or follow_ups_available():
        return metadata
    return {key: value for key, value in metadata.items()
            if key not in {"follow_ups", "follow_ups_generated"}}
