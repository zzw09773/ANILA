"""docling-service runtime settings.

docling 與 asr-decoder 同構:自家映像、權重來自掛載目錄、離線 only、健康
檢查要蓋得住分鐘級的冷啟動。改動理由見 2026-08-17 擁有者裁決(所有 GPU 工作
走上 HTTP 端點,不進平台映像)。
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service identity
    APP_NAME: str = "docling-service"
    APP_VERSION: str = "0.1.0"
    LOG_LEVEL: str = "INFO"
    ANILA_DEPLOYMENT_PROFILE: str = "development"

    # ── 共享祕密 ─────────────────────────────────────────────────────────
    # csp / ingestion-worker 的 DOCLING_SERVICE_TOKEN 要與這個一致;
    # 以 X-Token header 帶上。外部版部署(service 跑在獨立 GPU 主機、純 http
    # NodePort)時,這是唯一一道防線 → 必須夠長且不進 repo。
    DOCLING_SERVICE_TOKEN: str = ""

    @field_validator("DOCLING_SERVICE_TOKEN")
    @classmethod
    def _token_ascii_only(cls, v: str) -> str:
        # 非 ASCII token 在 X-Token header 編不了——unexpected 到的結果是
        # 「遠端無回應」，把組態錯誤指向網路。import 期就拒收、點名欄位。
        try:
            v.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError(
                "DOCLING_SERVICE_TOKEN must be ASCII-only; it carries the "
                "X-Token header, which cannot encode non-ASCII characters. "
                "Fix the setting — this is not a network or endpoint problem."
            ) from None
        return v

    # ── 權重 ────────────────────────────────────────────────────────────
    # 掛載目錄。內含 fetch-docling-weights.sh 產出的 layout/tableformer/
    # EasyOCR 權重(layout model + tableformer + easyocr)。
    DOCLING_ARTIFACTS_DIR: str = "/var/anila/docling-artifacts"

    # ── 離線 ────────────────────────────────────────────────────────────
    # 進氣隙內網後,docling 任何對 HF / docling artifact CDN 的出向請求都要
    # 擋掉,權重缺了就 fail loud(卡在 DNS timeout 會讓 health 永遠 not ready)。
    # 這是單一開關:model.py::apply_offline_environment 把它寫進
    # HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE(管 huggingface_hub 路徑),並把
    # EasyOcrOptions.download_enabled 設成 False(管 EasyOCR 的 url 直抓路徑)。
    # ⚠ 別再把 HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE 加回來當 Settings 欄位:
    # 它們只是 env 的名字,不是本服務會讀的設定——那是「讀起來像在控制離線、
    # 實際沒接線」的假控制項(2026-08-17 審查 HIGH)。
    DOCLING_LOCAL_FILES_ONLY: bool = True

    # ── OCR / 結構 ───────────────────────────────────────────────────────
    # 預設與現行 in-process 版一致:繁中 + 英、開表格結構、關圖片描述。
    DOCLING_OCR_LANGS: str = "ch_tra,en"
    DOCLING_TABLE_STRUCTURE: bool = True
    DOCLING_PICTURE_DESCRIPTION: bool = False

    # ── 資源上限 ────────────────────────────────────────────────────────
    # 單份文件可接受的位元組數。2 GB 是上限的極粗保險;正常文件遠小於此。
    DOCLING_MAX_FILE_BYTES: int = 2_000_000_000
    # 單次 parse 的 CPU/GPU 負載上限不在這顆設定;docling 沒有中斷 API,
    # 長文件讓它跑完(呼端以 DOCLING_TIMEOUT_SECONDS 保護)。


settings = Settings()
