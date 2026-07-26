// W2-4 ③ —— 串流錯誤 body 的使用者語彙映射。
//
// 缺陷本體:`runtime/sse.js` 把 `await response.text()` 的原始 body 直接當
// Error message 交給 UI,使用者看到的是一整串 JSON。這裡釘住:
//   ① 映射吃 `error.code`(W2-12 的結構化信封),**不比對中文子字串**;
//   ② 原文只進 console,不進 `error.message`;
//   ③ 未知碼 fallback 成「發生錯誤(代碼)+ 重試」。

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  STREAM_ERROR_MESSAGES,
  STATUS_FALLBACK_CODES,
  buildStreamError,
  classifyStreamError,
} from "../runtime/streamError.js";

afterEach(() => {
  vi.restoreAllMocks();
});

const envelope = (code, extra = {}) => ({
  error: { code, message: "server side message", details: null, request_id: null, ...extra },
  detail: "server side message",
});

describe("classifyStreamError:以 error.code 分流", () => {
  it("模型未註冊 → 使用者語彙提到模型與註冊,不含原始 JSON", () => {
    const raw = JSON.stringify(envelope("MODEL_NOT_REGISTERED"));
    const info = classifyStreamError({ status: 400, envelope: JSON.parse(raw), raw });
    expect(info.code).toBe("MODEL_NOT_REGISTERED");
    expect(info.known).toBe(true);
    expect(info.message).toContain("模型");
    expect(info.message).toContain("註冊");
    expect(info.message).not.toContain("{");
    expect(info.message).not.toContain("MODEL_NOT_REGISTERED");
  });

  it("逾時 → 使用者語彙提到逾時", () => {
    const info = classifyStreamError({ status: 504, envelope: envelope("UPSTREAM_TIMEOUT"), raw: "" });
    expect(info.message).toContain("逾時");
  });

  it("**不比對中文子字串**:server message 寫著「模型未註冊」但 code 是 FORBIDDEN → 走 FORBIDDEN 的語彙", () => {
    const info = classifyStreamError({
      status: 403,
      envelope: envelope("FORBIDDEN", { message: "模型未註冊" }),
      raw: "",
    });
    expect(info.code).toBe("FORBIDDEN");
    expect(info.message).toBe(STREAM_ERROR_MESSAGES.FORBIDDEN);
    expect(info.message).not.toContain("註冊");
  });

  it("未知碼 → fallback 帶代碼 + 重試指引", () => {
    const info = classifyStreamError({ status: 418, envelope: envelope("SOME_FUTURE_CODE"), raw: "" });
    expect(info.known).toBe(false);
    expect(info.message).toContain("SOME_FUTURE_CODE");
    expect(info.message).toContain("重試");
  });

  it("非 JSON body(nginx 502 HTML)→ 走 status fallback,不把 HTML 丟給使用者", () => {
    const raw = "<html><head><title>502 Bad Gateway</title></head></html>";
    const info = classifyStreamError({ status: 502, envelope: null, raw });
    expect(info.code).toBe(STATUS_FALLBACK_CODES[502]);
    expect(info.message).toBe(STREAM_ERROR_MESSAGES[STATUS_FALLBACK_CODES[502]]);
    expect(info.message).not.toContain("html");
  });

  it("完全沒對照的 status → HTTP_ERROR", () => {
    const info = classifyStreamError({ status: 599, envelope: null, raw: "" });
    expect(info.code).toBe("HTTP_ERROR");
  });

  it("request_id 帶進使用者訊息(使用者要回報得出來)", () => {
    const info = classifyStreamError({
      status: 500,
      envelope: envelope("INTERNAL_ERROR", { request_id: "req-abc123" }),
      raw: "",
    });
    expect(info.requestId).toBe("req-abc123");
    expect(info.message).toContain("req-abc123");
  });

  it("legacy-only body(只有 detail 沒有 error)→ 仍走 status fallback,detail 不進畫面", () => {
    const raw = JSON.stringify({ detail: "raw legacy detail string" });
    const info = classifyStreamError({ status: 503, envelope: JSON.parse(raw), raw });
    expect(info.code).toBe("SERVICE_UNAVAILABLE");
    expect(info.message).not.toContain("raw legacy detail string");
  });
});

describe("buildStreamError:原文進 console 不進畫面", () => {
  it("Error.message 是映射後的語彙;原始 body 只出現在 console.error", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const raw = JSON.stringify(envelope("MODEL_UNHEALTHY", { request_id: "req-9" }));
    const err = buildStreamError({ status: 503, envelope: JSON.parse(raw), raw });

    expect(err).toBeInstanceOf(Error);
    expect(err.message).not.toContain("server side message");
    expect(err.message).not.toContain("{");
    expect(err.code).toBe("MODEL_UNHEALTHY");
    expect(err.status).toBe(503);
    expect(err.requestId).toBe("req-9");
    expect(err.rawBody).toBe(raw);

    expect(spy).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(spy.mock.calls[0])).toContain("server side message");
  });

  it("空 body 也不炸,message 非空", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const err = buildStreamError({ status: 500, envelope: null, raw: "" });
    expect(err.message.length).toBeGreaterThan(0);
  });
});

describe("映射表與後端 ErrorCode 對齊", () => {
  // 後端 `services/csp/app/errors.py` 的分流碼(wire 值 == 成員名稱)。
  const BACKEND_ROUTING_CODES = [
    "AUTH_INVALID_CREDENTIALS", "AUTH_PENDING_APPROVAL", "AUTH_PENDING_REGISTRATION",
    "AUTH_LOCAL_PASSWORD_DISABLED", "AUTH_SOURCE_NOT_SUPPORTED", "AUTH_CSRF_INVALID",
    "MODEL_NOT_REGISTERED", "MODEL_UNHEALTHY", "AGENT_NOT_APPROVED",
    "CLEARANCE_INSUFFICIENT", "COMPARTMENT_DENIED", "CLASSIFICATION_FORBIDDEN",
    "QUOTA_EXCEEDED", "RATE_LIMITED", "UPSTREAM_TIMEOUT",
  ];
  const BACKEND_GENERIC_CODES = [
    "VALIDATION_ERROR", "BAD_REQUEST", "UNAUTHENTICATED", "FORBIDDEN", "NOT_FOUND",
    "CONFLICT", "PAYLOAD_TOO_LARGE", "INTERNAL_ERROR", "SERVICE_UNAVAILABLE", "HTTP_ERROR",
  ];

  for (const code of [...BACKEND_ROUTING_CODES, ...BACKEND_GENERIC_CODES]) {
    it(`${code} 有對應的使用者語彙`, () => {
      expect(STREAM_ERROR_MESSAGES[code]).toBeTruthy();
    });
  }

  it("status fallback 的每個值都在映射表裡", () => {
    for (const code of Object.values(STATUS_FALLBACK_CODES)) {
      expect(STREAM_ERROR_MESSAGES[code]).toBeTruthy();
    }
  });
});
