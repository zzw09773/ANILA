"""Int16 PCM → WAV(RIFF)封裝。

原生 ANILA 契約送的是**無標頭**的 Int16 mono PCM(`decode_client` 直接把
bytes 當 `application/octet-stream` 貼上去),因為 decoder 那端寫死了
16 kHz / mono / int16。OpenAI 相容的 `/v1/audio/transcriptions` 是 multipart
檔案上傳 —— 對方只能從檔頭知道取樣率、聲道數與位元深度。

⚠ 少了這 44 bytes 不是「格式漂亮一點」的問題:多數相容端點會直接回 400,
但**寬鬆的實作會照自己的預設取樣率去解**,結果是能讀、但語速全錯的逐字稿。
後者是靜默錯誤,比 400 貴得多 —— 所以 framing 由我們負責,不靠對方猜。

只用標準函式庫 `wave`,不引新相依(氣隙 bundle 少一個輪子)。
"""

from __future__ import annotations

import io
import wave

# 與 app/transcriber.py 的 SAMPLE_RATE 同義。這裡刻意再寫一次而不是 import,
# 因為 WAV 檔頭是**對外契約**:改取樣率必須是有意識的決定,不是跟著別的模組漂移。
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # int16

_FRAME_BYTES = CHANNELS * SAMPLE_WIDTH_BYTES


def pcm16_to_wav(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """把無標頭 Int16 mono PCM 包成 WAV。

    長度不是整數個取樣時直接 raise —— 半個取樣代表上游切錯,悄悄補零只會
    讓錯誤延後到「聽起來怪怪的」才被發現。
    """
    if len(pcm) % _FRAME_BYTES:
        raise ValueError(
            f"PCM 長度 {len(pcm)} 不是 int16 mono 取樣的整數倍(每取樣 "
            f"{_FRAME_BYTES} bytes)"
        )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def silence_wav(milliseconds: int = 100) -> bytes:
    """健康探針用的極短靜音 WAV。

    探針要**真的**打一次辨識端點(才驗得到金鑰),但不該讓遠端算真的音訊。
    100 ms 靜音是各家 whisper 相容端點普遍接受的最短長度;更短的常被回 400,
    而 400 在探針語意裡等同「金鑰通過、只是這段太短」,所以兩種都能判讀。
    """
    frames = int(SAMPLE_RATE * max(milliseconds, 0) / 1000)
    return pcm16_to_wav(b"\x00\x00" * frames)
