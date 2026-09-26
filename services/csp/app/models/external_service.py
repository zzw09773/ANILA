# -*- coding: utf-8 -*-
"""治理中心管理的外部服務。

為什麼是這一張小表
==================
文件解析（Docling）與語音辨識（ASR 解碼器）欄位相同：位址、可選憑證、
啟用、健康。維護者要在主控台改位址，不能改 .env、也不能重建映像。

``platform_settings`` 的 value 是明文純字串，總覽 API 會整列回出去，
不能放憑證。``model_registry`` 是推理模型（協議、分類上限、思考等級）；
文件解析不是模型，語音位址搬家時也不該逼人去填那些欄位。

憑證不是模型 API key 那把 ``SECRET_KEY``。worker 持有那把金鑰，也連得上
這張表（同一個 ``csp_app``，表的擁有者撤不掉自己的 SELECT）。語音憑證改用
CSP 自己產生、只掛在 CSP 的金鑰檔（``enc::ext1::``）。worker 改走限定的
內部 API 拿文件解析，不再讀這張表。
API 只回 ``has_credential``。明文只給對得上的那個呼叫端。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base

DOCUMENT_PARSER = "document_parser"
SPEECH = "speech"
SERVICE_KEYS = (DOCUMENT_PARSER, SPEECH)

# 已從平台 compose 拿掉的本機解碼器。環境變數還指著它時不要匯入。
RETIRED_LOCAL_ASR_HOST = "asr-decoder"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExternalService(Base):
    __tablename__ = "external_services"

    service_key = Column(String(40), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    base_url = Column(String(500), nullable=False, default="")
    # enc::ext1::。NULL 表示沒有憑證。不是 SECRET_KEY 那套 enc::v1::。
    credential_envelope = Column(Text, nullable=True)
    # speech：native | openai。document_parser 不使用，留 native。
    protocol = Column(String(20), nullable=False, default="native")
    openai_model = Column(String(120), nullable=False, default="whisper-1")
    health_status = Column(String(20), nullable=False, default="unknown")
    health_checked_at = Column(DateTime(timezone=True), nullable=True)
    health_detail = Column(Text, nullable=True)
    # 舊環境變數只匯入一次。管理員存過之後也是 true，重啟不會把 .env 寫回來。
    env_seeded = Column(Boolean, nullable=False, default=False)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    updated_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
