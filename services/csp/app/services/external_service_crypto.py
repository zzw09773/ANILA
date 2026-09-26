# -*- coding: utf-8 -*-
"""外部服務憑證的專用金鑰。

ingestion-worker 跟 CSP 共用 ``SECRET_KEY``，也用同一個資料庫帳號。
那把金鑰再拿來包 Docling／語音憑證的話，worker 讀得到表就能解開語音憑證。
資料庫角色拆不開：遷移用的帳號把表授給 ``csp_app``，CSP 與 worker 都是它。

所以這把金鑰只放在 CSP 自己的目錄。第一次啟動若檔案不在，就產生 32 bytes，
不寫進 .env，維護者不用另建祕密。worker 沒有這個掛載，也沒有這支模組。
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from anila_core.security.credential_crypto import unpack_credential_envelope

logger = logging.getLogger(__name__)

# 跟模型 API key 的 enc::v1:: 分開。worker 那支解法只認舊前綴。
PREFIX = "enc::ext1::"
LEGACY_PREFIX = "enc::v1::"
_KEY_BYTES = 32
_DEFAULT_PATH = Path("/var/anila/csp-local-secrets/external-service.key")


def key_file() -> Path:
    raw = os.environ.get("ANILA_EXTERNAL_SERVICE_KEY_FILE", "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def load_or_create_master_key() -> bytes:
    """讀專用金鑰。沒有檔就產生。內容不進日誌。"""
    path = key_file()
    if path.is_file():
        data = path.read_bytes()
        if len(data) != _KEY_BYTES:
            raise RuntimeError("外部服務金鑰檔長度不對")
        return data
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(_KEY_BYTES)
    temporary = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        existing = path.read_bytes()
        if len(existing) != _KEY_BYTES:
            raise RuntimeError("外部服務金鑰檔長度不對")
        return existing
    os.chmod(path, 0o600)
    logger.info("external_services: 已建立專用金鑰檔 %s", path)
    return data


def encrypt_external_credential(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("無法將空字串編碼為外部服務憑證")
    key = load_or_create_master_key()
    nonce = os.urandom(12)
    blob = nonce + AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def open_external_credential(stored: str | None) -> str | None:
    """解開專用外殼。舊的 ``enc::v1::`` 仍可用 CSP 的 SECRET_KEY 讀一次，供改寫。"""
    if not stored:
        return None
    if stored.startswith(LEGACY_PREFIX):
        return unpack_credential_envelope(stored)
    if not stored.startswith(PREFIX):
        raise ValueError("外部服務憑證外殼不對")
    raw = base64.urlsafe_b64decode(stored[len(PREFIX):].encode("ascii"))
    if len(raw) < 12 + 16:
        raise ValueError("外部服務憑證過短")
    nonce, ciphertext = raw[:12], raw[12:]
    key = load_or_create_master_key()
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
