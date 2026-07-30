"""單一 ASR session 的 async 編排:VAD event → 解碼 → WS 訊息。

這一層負責規劃書 §2.2「async 編排層必守規則」的七條。核心是**單一 worker
task**:它讓「同時最多一個 in-flight 解碼」成為結構上的保證,而不是靠紀律
維持的約定 —— 原版是靠「每 session 一條 thread + Condition」達到同一件事。

三個容易被誤解的地方:

1. **partial 的 utt_id 在解碼那一刻才決定**,不是 `PartialReady` 事件裡的那個。
   事件排隊期間語段可能已收尾、下一段已開始 —— 沿用事件裡的舊 id 會把新句子
   的預覽蓋到舊句子上。所以 pending 只是一個旗標,真正要解時才向 segmenter
   同時拿 `current_utt_id` 與 `partial_snapshot()`。原版同款。
2. **stale partial 是真的會發生**,即使 worker 是單執行緒的:`Discard` 來自
   `feed()`(WS 接收迴圈),與 worker 的 await 並行。partial 解碼途中該 utt
   可能已被 discard 掉 → 回來時必須丟棄,否則預覽文字會在定稿後復活且永遠
   清不掉(協定不變式,見 §2.2)。
3. **棄單不會被取消**:httpx timeout 放棄的 partial,decoder 仍在 lock 裡解完。
   規則 1(final 優先)與 2(單 in-flight + 塌縮)就是為了讓棄單數量有上界。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable

import numpy as np

from app.decode_client import KIND_FINAL, KIND_PARTIAL, DecodeError
from app.transcriber import (
    PARTIAL_MIN_AVG_LOGPROB,
    PARTIAL_NO_SPEECH_PROB,
    SAMPLE_RATE,
    Discard,
    FinalReady,
    PartialReady,
    SpeechEnd,
    SpeechStart,
    VadSegmenter,
    is_hallucination,
    looks_degenerate,
)

logger = logging.getLogger(__name__)

Send = Callable[[dict], Awaitable[None]]
Decode = Callable[..., Awaitable[dict]]


def make_text_filter(mode: str) -> Callable[[str], str]:
    """繁體後處理。預設 `off` = 原樣回傳。

    ⚠ 不要預設開:實測 whisper medium 在 1096 句真實繁中語料的簡體率是 0%,
    而 OpenCC 對正確繁體的誤傷率 6~10%(見規劃書 §10)。opencc 不在 runtime
    相依裡,設了非 off 卻沒裝就 ImportError —— 這是刻意的 fail-loud。
    """
    if mode == "off":
        return lambda text: text
    if mode == "s2twp":
        raise ValueError(
            "ASR_OPENCC_MODE=s2twp 被明令禁止:實測對正確繁體誤傷 10.58%"
            "(會把「類型」轉成「型別」)。見規劃書 §10。"
        )
    import opencc  # noqa: PLC0415 — 只在真的要用時才 import

    converter = opencc.OpenCC(mode)
    return converter.convert


class AsrSession:
    """一條 WS 連線的辨識狀態。呼叫端負責 WS 收發與生命週期。"""

    def __init__(
        self,
        *,
        decode: Decode,
        send: Send,
        segmenter: VadSegmenter | None = None,
        initial_prompt: str | None = "以下是繁體中文。",
        beam_size: int = 5,
        partials_enabled: bool = True,
        text_filter: Callable[[str], str] | None = None,
    ) -> None:
        self._segmenter = segmenter or VadSegmenter(partials_enabled=partials_enabled)
        self._decode = decode
        self._send = send
        self._initial_prompt = initial_prompt
        self._beam_size = beam_size
        self._partials_enabled = partials_enabled
        self._text_filter = text_filter or (lambda t: t)

        self._finals: deque[FinalReady] = deque()
        # 已從佇列取出、但還在解碼中的 final。busy 必須把它算進去 —— 只看
        # 佇列的話,worker 一 pop 佇列就空了,drain() 會在解碼還在飛的時候
        # 就返回,然後我們關掉 WS,使用者最後一句話默默消失(正是規則 6 要
        # 防的事)。
        self._final_in_flight = False
        self._partial_pending = False
        # 已送出 final 或 discard 的 utt。遲到的 partial 靠它擋下。
        self._terminal: set[int] = set()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._stopped = False

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._run(), name="asr-session-worker")

    async def aclose(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    @property
    def busy(self) -> bool:
        """還有 final 沒解完(逾時收尾時要等它)——含正在飛的那一個。"""
        return bool(self._finals) or self._final_in_flight

    # ── 進料 ────────────────────────────────────────────────────────────

    async def feed(self, data: bytes) -> None:
        await self._handle(self._segmenter.feed(data))

    async def flush(self) -> None:
        """使用者按停 / session 逾時收尾:把還在講的那段強制定稿。"""
        await self._handle(self._segmenter.flush())

    async def drain(self, timeout: float) -> None:
        """等待佇列中的 final 解完(有上限)。逾時斷線前呼叫,否則使用者的
        最後一句話會默默消失。"""
        deadline = time.monotonic() + timeout
        while self.busy and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def _handle(self, events: list) -> None:
        for event in events:
            if isinstance(event, SpeechStart):
                await self._send({"type": "status", "state": "recording", "msg": "辨識中…"})
            elif isinstance(event, SpeechEnd):
                await self._send({"type": "status", "state": "listening", "msg": "聆聽中…"})
            elif isinstance(event, Discard):
                await self._mark_discarded(event.utt_id)
            elif isinstance(event, FinalReady):
                self._finals.append(event)
                self._wake.set()
            elif isinstance(event, PartialReady):
                if self._partials_enabled:
                    # 只是旗標:真正要解的那一刻才向 segmenter 拿 id 與音訊。
                    # 多個 PartialReady 自然塌縮成一次。
                    self._partial_pending = True
                    self._wake.set()

    async def _mark_discarded(self, utt_id: int) -> None:
        self._terminal.add(utt_id)
        await self._send({"type": "discard", "id": utt_id})

    # ── worker ──────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while not self._stopped:
            job = self._next_job()
            if job is None:
                await self._wake.wait()
                self._wake.clear()
                continue
            try:
                await job
            except asyncio.CancelledError:
                raise
            except Exception:
                # 一個 utt 解爆不該讓整條 session 死掉。
                logger.exception("decode job failed")

    def _next_job(self):
        """final 優先;有 final 待解時完全不碰 partial(規則 1)。

        `_final_in_flight` 在這裡就設起來(而不是在 coroutine 內),因為 pop
        與 coroutine 開始執行之間雖然沒有 await、理論上沒有觀察窗,但把旗標
        與 pop 綁在同一個原子步驟裡,之後有人在中間插入 await 也不會破功。
        """
        if self._finals:
            self._final_in_flight = True
            return self._decode_final(self._finals.popleft())
        if self._partial_pending:
            self._partial_pending = False
            return self._decode_partial()
        return None

    async def _decode_final(self, event: FinalReady) -> None:
        try:
            await self._decode_final_inner(event)
        finally:
            self._final_in_flight = False

    async def _decode_final_inner(self, event: FinalReady) -> None:
        samples = _to_samples(event.audio)
        try:
            result = await self._decode(
                samples,
                kind=KIND_FINAL,
                prompt=self._initial_prompt,
                beam=self._beam_size,
            )
        except DecodeError as exc:
            self._terminal.add(event.utt_id)
            await self._send({"type": "error", "msg": str(exc)})
            await self._send({"type": "discard", "id": event.utt_id})
            return

        raw = (result.get("text") or "").strip()
        if not raw or is_hallucination(raw):
            await self._mark_discarded(event.utt_id)
            return

        self._terminal.add(event.utt_id)
        await self._send({
            "type": "final",
            "id": event.utt_id,
            "text": self._text_filter(raw),
            "duration_seconds": len(samples) / SAMPLE_RATE,
            "decode_seconds": float(result.get("decode_seconds", 0.0)),
            "latency_seconds": time.monotonic() - event.ended_at,
        })

    async def _decode_partial(self) -> None:
        # id 與音訊必須同時取(見檔頭第 1 點)。
        utt_id = self._segmenter.current_utt_id
        audio = self._segmenter.partial_snapshot()
        if not audio or utt_id in self._terminal:
            return

        samples = _to_samples(audio)
        result = await self._decode(
            samples, kind=KIND_PARTIAL, prompt=self._initial_prompt, beam=self._beam_size
        )

        # 解碼期間該 utt 可能已被 discard(Discard 來自 feed(),與這裡的 await
        # 並行)→ 丟棄,否則預覽會在定稿後復活(協定不變式)。
        if utt_id in self._terminal:
            return

        raw = (result.get("text") or "").strip()
        # 半句話本來就長得像雜訊,兩個指標都難看才丟 —— 單一指標會誤殺。
        if (result.get("no_speech_prob", 0.0) > PARTIAL_NO_SPEECH_PROB
                and result.get("avg_logprob", 0.0) < PARTIAL_MIN_AVG_LOGPROB):
            return
        if not raw or is_hallucination(raw) or looks_degenerate(raw):
            return

        await self._send({
            "type": "partial",
            "id": utt_id,
            "text": self._text_filter(raw),
        })


def _to_samples(audio: bytes) -> np.ndarray:
    return np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
