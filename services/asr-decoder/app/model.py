"""faster-whisper 包裝:PCM samples → 文字。

無狀態、無 session 概念 —— 切句與 partial/final 的節奏全在 asr-gateway,
這裡只負責「收一段 samples,回一段文字」。

解碼參數沿用 ``語料庫/ASR_intranet/code_for_mlsteam/streaming_asr/app.py``
已在內網驗證過的兩組設定,不要憑感覺改。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from app.config import Settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

KIND_PARTIAL = "partial"
KIND_FINAL = "final"


def _resolve_model_ref(settings: Settings) -> tuple[str, str | None]:
    """回 (model_size_or_path, download_root)。

    ASR_MODEL_DIR 內若已經是一份轉檔好的 CTranslate2 模型(認 model.bin),
    就直接把目錄當模型路徑 —— air-gap bundle 是這樣掛進來的。否則把它當
    下載快取根目錄,模型仍以 ASR_MODEL_SIZE 的名稱解析。
    """
    model_dir = settings.ASR_MODEL_DIR.strip()
    if not model_dir:
        return settings.ASR_MODEL_SIZE, None
    if (Path(model_dir) / "model.bin").is_file():
        return model_dir, None
    return settings.ASR_MODEL_SIZE, model_dir


class WhisperDecoder:
    """單一 model instance;所有 decode 以一把 lock 序列化。

    一張 GPU 同時只跑一個 decode 是刻意的:併發解碼互搶 VRAM 與 SM 只會讓
    每一條都變慢,延遲反而更難預測。要擴充併發是之後的事(多 instance 或
    多卡),不是在這裡放掉 lock。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: Any = None
        self._lock = threading.Lock()
        self._ready = threading.Event()

    # ── lifecycle ───────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def info(self) -> dict[str, Any]:
        model_ref, _ = _resolve_model_ref(self._settings)
        return {
            "model": model_ref,
            "device": self._settings.ASR_DEVICE,
            "compute_type": self._settings.ASR_COMPUTE_TYPE,
            "ready": self.ready,
        }

    def load(self) -> None:
        """載入權重並暖機。阻塞;由背景執行緒呼叫,ready 之前 /health 回 503。"""
        from faster_whisper import WhisperModel

        model_ref, download_root = _resolve_model_ref(self._settings)
        kwargs: dict[str, Any] = {
            "device": self._settings.ASR_DEVICE,
            "compute_type": self._settings.ASR_COMPUTE_TYPE,
            "local_files_only": self._settings.ASR_LOCAL_FILES_ONLY,
        }
        if download_root:
            kwargs["download_root"] = download_root
        if self._settings.ASR_DEVICE == "cpu" and self._settings.ASR_CPU_THREADS > 0:
            kwargs["cpu_threads"] = self._settings.ASR_CPU_THREADS

        logger.info("loading model=%s device=%s compute_type=%s",
                    model_ref, self._settings.ASR_DEVICE,
                    self._settings.ASR_COMPUTE_TYPE)
        started = time.perf_counter()
        model = WhisperModel(model_ref, **kwargs)

        # 暖機:第一次 decode 會觸發 CUDA context 建立與 kernel autotune,
        # 沒暖機的話第一個真實使用者要多等好幾秒。跑半秒靜音就夠。
        list(model.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32),
                              language="zh")[0])

        self._model = model
        self._ready.set()
        logger.info("model ready in %.1fs", time.perf_counter() - started)

    # ── decode ──────────────────────────────────────────────────────────

    def transcribe(
        self,
        samples: np.ndarray,
        *,
        kind: str,
        prompt: str | None,
        beam: int,
        language: str,
    ) -> dict[str, Any]:
        if self._model is None:
            raise RuntimeError("model not loaded")

        options: dict[str, Any] = {
            "language": language,
            "task": "transcribe",
            # ⚠ 不要開:faster-whisper 內建 VAD 會去抓 silero onnx,air-gap 直接炸。
            # 切句已經由 gateway 的 webrtcvad 做完了,這裡再過一次也沒意義。
            "vad_filter": False,
            # 每段獨立解碼。開了會讓前一句的錯誤污染下一句,串流場景下
            # 幻覺會滾雪球。
            "condition_on_previous_text": False,
            "initial_prompt": prompt,
            "without_timestamps": True,
        }
        if kind == KIND_PARTIAL:
            # Partial 每 ~0.5s 就重出一次,壽命極短 → 用 greedy 搶速度,
            # 並關掉所有品質門檻:半句話本來就長得像雜訊,讓 whisper 自己
            # 判斷「這不是語音」會把正在講的句子整個吃掉。過濾交給 gateway。
            options.update(
                beam_size=1,
                temperature=0.0,
                no_speech_threshold=None,
                compression_ratio_threshold=None,
                log_prob_threshold=None,
            )
        else:
            # Final 會留在畫面上 → 花得起 beam search,也該把門檻打開擋幻覺。
            options.update(
                beam_size=beam,
                no_speech_threshold=0.5,
                compression_ratio_threshold=2.2,
                log_prob_threshold=-1.0,
            )

        started = time.perf_counter()
        with self._lock:
            segments, _info = self._model.transcribe(samples, **options)
            seg = list(segments)  # generator — 必須在 lock 內跑完才算解碼結束
        return {
            "text": "".join(s.text.strip() for s in seg).strip(),
            "no_speech_prob": max((s.no_speech_prob for s in seg), default=0.0),
            "avg_logprob": (sum(s.avg_logprob for s in seg) / len(seg)) if seg else 0.0,
            "decode_seconds": time.perf_counter() - started,
        }
