"""會**挑剔**的假解碼端 —— 真的開一個 socket,真的檢查送進來的位元組。

為什麼不是 respx:respx 對「有沒有打到這個 URL」以外的一切照單全收。欄位
名打錯、檔案的 Content-Type 打錯、`Authorization` 整個不送、把裸 PCM 當成
WAV 送上去 —— respx 全部回 200,而測試只斷言「有呼叫」,所以全綠。實測九個
這類突變在 155 條測試底下一條都不會紅,其中兩個是活體致命的:欄位名改成
`audio` 讓每一句話 422、探針不送 `Authorization` 讓 `/asr/health` 對著一把
**正確的**金鑰喊 `decoder_unauthorized`(綠燈的測試、藏起來的麥克風、被派去
輪替憑證的 operator)。

所以這一份:
  * 是真的 HTTP server(`127.0.0.1:0`,執行緒裡跑),客戶端走完整的 httpx
    傳輸層 —— 斷言的是**離開 process 的位元組**,不是呼叫參數。
  * **會拒絕**:憑證缺/scheme 錯/值錯 → 401;檔案部件的 Content-Type 錯、
    音訊不是 16 kHz mono 16-bit WAV → 415;缺 `file` 或缺 `model` → 422。
  * **會記錄**:每一次請求的 header、query、表單欄位、檔案部件都留著,測試
    可以直接斷言。探針那條路特別需要記錄 —— 探針刻意把 400/415/422 當成
    「認證過了、只是嫌這段 100 ms 靜音」(見 decode_probe._probe_openai),
    所以「被拒絕」不會反映在 probe 的結論上,只能從記錄看。

⚠ 這個檔本身沒有測試函式,是給 test module import 的工具。
"""

from __future__ import annotations

import io
import json
import re
import threading
import wave
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SAMPLE_RATE = 16_000


@dataclass
class Part:
    """multipart 的一個部件。"""

    name: str
    filename: str | None
    content_type: str | None
    content: bytes


@dataclass
class Recorded:
    """一次進來的請求。祕密照記 —— 這是測試程序,不是服務。"""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    parts: list[Part] = field(default_factory=list)
    rejected: str | None = None

    # ── 方便斷言的取用器 ────────────────────────────────────────────────
    def part(self, name: str) -> Part | None:
        for p in self.parts:
            if p.name == name:
                return p
        return None

    def form(self, name: str) -> str | None:
        p = self.part(name)
        return None if p is None else p.content.decode("utf-8")

    @property
    def part_names(self) -> list[str]:
        return [p.name for p in self.parts]

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;]+))', re.IGNORECASE)
_NAME_RE = re.compile(r'name="([^"]*)"')
_FILENAME_RE = re.compile(r'filename="([^"]*)"')


def parse_multipart(body: bytes, content_type: str) -> list[Part]:
    """手工拆 multipart。

    刻意不用 `email` / `cgi`:那兩個會對 payload 做文字層的處理,而我們要驗的
    正是「送出去的二進位內容一個位元組都沒變」。
    """
    match = _BOUNDARY_RE.search(content_type or "")
    if not match:
        return []
    boundary = (match.group(1) or match.group(2)).strip().encode("ascii")
    delimiter = b"--" + boundary
    parts: list[Part] = []
    for segment in body.split(delimiter)[1:]:
        if segment.startswith(b"--"):  # 收尾的 `--boundary--`
            break
        segment = segment.lstrip(b"\r\n")
        head, sep, payload = segment.partition(b"\r\n\r\n")
        if not sep:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        headers: dict[str, str] = {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                key, value = line.split(b":", 1)
                headers[key.decode("latin-1").strip().lower()] = (
                    value.decode("latin-1").strip()
                )
        disposition = headers.get("content-disposition", "")
        name_match = _NAME_RE.search(disposition)
        filename_match = _FILENAME_RE.search(disposition)
        parts.append(
            Part(
                name=name_match.group(1) if name_match else "",
                filename=filename_match.group(1) if filename_match else None,
                content_type=headers.get("content-type"),
                content=payload,
            )
        )
    return parts


def wav_facts(blob: bytes) -> dict:
    """解析 WAV;不是合法的 16 kHz / mono / 16-bit 就 raise。"""
    with wave.open(io.BytesIO(blob), "rb") as handle:
        facts = {
            "channels": handle.getnchannels(),
            "sampwidth": handle.getsampwidth(),
            "framerate": handle.getframerate(),
            "frames": handle.getnframes(),
        }
    if facts["channels"] != 1 or facts["sampwidth"] != 2:
        raise ValueError(f"不是 mono 16-bit: {facts}")
    if facts["framerate"] != SAMPLE_RATE:
        raise ValueError(f"取樣率不是 {SAMPLE_RATE}: {facts}")
    return facts


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StrictDecoderStub/1"

    # 測試輸出不要被 stderr 的 access log 淹掉。
    def log_message(self, fmt, *args):  # noqa: D102, N802
        pass

    # ── 共用 ────────────────────────────────────────────────────────────
    def _read(self) -> Recorded:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parsed = urlparse(self.path)
        record = Recorded(
            method=self.command,
            path=parsed.path,
            query=parse_qs(parsed.query),
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
        )
        ctype = record.header("content-type") or ""
        if ctype.startswith("multipart/form-data"):
            record.parts = parse_multipart(body, ctype)
        return record

    def _reply(self, record: Recorded, status: int, payload: dict) -> None:
        if status >= 400:
            record.rejected = f"HTTP {status}: {payload}"
        self.server.stub.record(record)
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    # ── 路由 ────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        record = self._read()
        if record.path == "/health":
            return self._reply(record, 200, {"status": "ok"})
        return self._reply(record, 404, {"error": f"no route {record.path}"})

    def do_POST(self) -> None:  # noqa: N802
        record = self._read()
        forced = self.server.stub.forced_status
        if forced is not None:
            return self._reply(record, forced, {"error": self.server.stub.forced_body})
        if record.path == "/transcribe":
            return self._native(record)
        if record.path == "/v1/audio/transcriptions":
            return self._openai(record)
        return self._reply(record, 404, {"error": f"no route {record.path}"})

    # ── 原生契約 ────────────────────────────────────────────────────────
    def _native(self, record: Recorded) -> None:
        stub = self.server.stub
        token = record.header("x-token")
        if token is None:
            return self._reply(record, 401, {"error": "missing X-Token"})
        if token != stub.credential:
            return self._reply(record, 401, {"error": "wrong X-Token"})
        ctype = (record.header("content-type") or "").split(";")[0].strip()
        if ctype != "application/octet-stream":
            return self._reply(
                record, 415, {"error": f"body must be application/octet-stream, got {ctype!r}"}
            )
        if len(record.body) % 2:
            return self._reply(record, 400, {"error": "PCM 位元組數必須是偶數(int16)"})
        return self._reply(
            record,
            200,
            {
                "text": stub.transcript,
                "no_speech_prob": 0.01,
                "avg_logprob": -0.2,
                "decode_seconds": 0.05,
            },
        )

    # ── OpenAI 相容契約 ────────────────────────────────────────────────
    def _openai(self, record: Recorded) -> None:
        stub = self.server.stub
        authorization = record.header("authorization")
        if authorization is None:
            return self._reply(record, 401, {"error": "missing Authorization"})
        scheme, _, value = authorization.partition(" ")
        if scheme != "Bearer":
            return self._reply(
                record, 401, {"error": f"Authorization scheme must be Bearer, got {scheme!r}"}
            )
        if value != stub.credential:
            return self._reply(record, 401, {"error": "invalid api key"})

        ctype = record.header("content-type") or ""
        if not ctype.startswith("multipart/form-data"):
            return self._reply(record, 415, {"error": f"expected multipart, got {ctype!r}"})

        audio = record.part("file")
        if audio is None:
            return self._reply(
                record,
                422,
                {"error": f"missing form field 'file' (got {record.part_names})"},
            )
        part_ctype = (audio.content_type or "").split(";")[0].strip()
        if part_ctype != "audio/wav":
            return self._reply(
                record,
                415,
                {"error": f"file part must be audio/wav, got {part_ctype!r}"},
            )
        try:
            wav_facts(audio.content)
        except Exception as exc:  # noqa: BLE001
            return self._reply(record, 415, {"error": f"unusable audio: {exc}"})

        model = record.form("model")
        if not model:
            return self._reply(record, 422, {"error": "missing form field 'model'"})
        if stub.model is not None and model != stub.model:
            return self._reply(record, 422, {"error": f"unknown model {model!r}"})

        return self._reply(record, 200, {"text": stub.transcript})


class StrictDecoderStub:
    """context manager:`with StrictDecoderStub(credential=…) as stub:`"""

    def __init__(
        self,
        *,
        credential: str,
        model: str | None = None,
        transcript: str = "測試語音",
    ) -> None:
        self.credential = credential
        self.model = model
        self.transcript = transcript
        # 測試要模擬「上游壞掉」時用:非 None 就對所有 POST 回這個狀態碼。
        self.forced_status: int | None = None
        self.forced_body: str = "boom"
        self.calls: list[Recorded] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.stub = self  # handler 讀得到
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="strict-decoder-stub", daemon=True
        )

    # ── 生命週期 ────────────────────────────────────────────────────────
    def __enter__(self) -> "StrictDecoderStub":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # ── 記錄 ────────────────────────────────────────────────────────────
    def record(self, entry: Recorded) -> None:
        with self._lock:
            self.calls.append(entry)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def url_with_userinfo(self, user: str, secret: str) -> str:
        """operator 把憑證貼進位址時的樣子(§3 的洩漏路徑)。"""
        host, port = self._server.server_address[:2]
        return f"http://{user}:{secret}@{host}:{port}"

    @property
    def last(self) -> Recorded:
        with self._lock:
            assert self.calls, "假解碼端一次請求都沒收到"
            return self.calls[-1]

    def last_to(self, path: str) -> Recorded:
        with self._lock:
            for entry in reversed(self.calls):
                if entry.path == path:
                    return entry
        raise AssertionError(
            f"假解碼端沒收到任何 {path} 請求(收到的是 "
            f"{[c.path for c in self.calls]})"
        )

    @property
    def rejections(self) -> list[str]:
        with self._lock:
            return [c.rejected for c in self.calls if c.rejected]
