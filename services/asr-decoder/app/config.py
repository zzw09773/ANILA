"""asr-decoder runtime settings.

模型尺寸 / 裝置 / 精度全部走 env,刻意不寫死:正式環境要用哪個尺寸取決於
部署當下 GPU 的 VRAM 餘裕,換模型不該動到程式碼(見
``docs/planning/asr-voice-input-plan.md`` §8)。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service identity
    APP_NAME: str = "asr-decoder"
    APP_VERSION: str = "0.1.0"
    LOG_LEVEL: str = "INFO"
    ANILA_DEPLOYMENT_PROFILE: str = "development"

    # ── 模型 ────────────────────────────────────────────────────────────
    # "medium" / "large-v3" 之類的尺寸名,或一個已轉檔的 CTranslate2 模型
    # 目錄絕對路徑。dev 預設 medium(下載快、CPU 也跑得動);正式建議
    # large-v3(繁中正確率差距在中文場景比英文明顯)。
    ASR_MODEL_SIZE: str = "medium"
    # 權重目錄。air-gap 部署以 volume 掛入並搭配 ASR_LOCAL_FILES_ONLY=1;
    # 目錄內若直接是轉檔好的模型(含 model.bin),就直接當模型路徑用,
    # 不再走 HuggingFace 的 repo 名稱解析。
    ASR_MODEL_DIR: str = ""
    # air-gap 一定要開:禁止任何對 HuggingFace 的出向請求,權重缺了就
    # fail loud,而不是卡在 DNS timeout 讓 health 一直不 ready。
    ASR_LOCAL_FILES_ONLY: bool = False

    # cuda / cpu。cpu 只適合 dev 與契約測試(large-v3 在 CPU 上 RTF≈1,
    # 互動場景不可行)。
    ASR_DEVICE: str = "cuda"
    # ⚠ V100(Volta)請用 float16,不要用 int8:Volta 沒有 int8 tensor core,
    # int8 走一般 cuBLAS 路徑,省了 VRAM 但速度反而更慢(faster-whisper 官方
    # V100S benchmark:fp16 54s vs int8 59s)。int8 的價值在 Turing 之後。
    ASR_COMPUTE_TYPE: str = "float16"
    # 只在 ASR_DEVICE=cpu 時有意義;0 = 交給 CTranslate2 自己決定。
    ASR_CPU_THREADS: int = 0

    # ── 認證 ────────────────────────────────────────────────────────────
    # 與 asr-gateway 共享的密鑰,gateway 以 X-Token header 帶上。
    # 外部版部署(decoder 跑在獨立 GPU 主機、走純 http NodePort)時,
    # 這是唯一的一道防線 → 必須夠長且不進 repo。
    ASR_DECODER_TOKEN: str = ""

    # ── 資源上限 ────────────────────────────────────────────────────────
    # 單次 decode 可接受的最長音訊。gateway 的切句上限是 30s
    # (MAX_UTTERANCE_SEC),留一倍餘裕擋畸形請求。
    ASR_MAX_AUDIO_SECONDS: float = 60.0


settings = Settings()
