// Slice 4d — fetchTrace resilience tests (mirror tasks.test.js conventions).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchTrace } from "../runtime/traces.js";

let warnSpy;

beforeEach(() => {
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("fetchTrace", () => {
  it("GETs /api/traces/{id} with credentials and returns the payload", async () => {
    const payload = {
      trace_id: "tr-1",
      task_id: 42,
      spans: [{ span_id: "s1", parent_span_id: null }],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchTrace("tr-1");

    expect(result).toEqual(payload);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/api/traces/tr-1");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
  });

  it("url-encodes the trace id", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ trace_id: "a/b", task_id: 1, spans: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await fetchTrace("a/b");
    expect(fetchMock.mock.calls[0][0]).toContain("/api/traces/a%2Fb");
  });

  it("returns null without fetching for an empty/blank id", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchTrace("")).toBeNull();
    expect(await fetchTrace("   ")).toBeNull();
    expect(await fetchTrace(null)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns null and warns (zh-TW) on 404", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));

    expect(await fetchTrace("missing")).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("取得軌跡失敗（HTTP 404）"),
    );
  });

  it("returns null and warns (zh-TW) on 403 foreign trace", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403 }));

    expect(await fetchTrace("someone-elses")).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("取得軌跡失敗（HTTP 403）"),
    );
  });

  it("returns null and warns (zh-TW) on network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("failed")));

    expect(await fetchTrace("tr-1")).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("網路錯誤"),
      expect.any(TypeError),
    );
  });

  it("returns null when the response lacks a spans array", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ trace_id: "x" }) }),
    );

    expect(await fetchTrace("tr-1")).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("缺少 spans 欄位"),
    );
  });
});
