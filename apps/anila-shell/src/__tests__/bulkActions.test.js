import { describe, expect, it, vi } from "vitest";

import { authRequest } from "../runtime/api.js";
import {
  BULK_CONCURRENCY,
  SUMMARY_BULK_MAX,
  appendRemoteHits,
  bindExpectedUser,
  chunkPositiveIds,
  deleteSummaryBatches,
  mapLimited,
  moveFolderOptions,
  streamingConversationIds,
} from "../runtime/bulkActions.js";

describe("bulkActions", () => {
  it("同時執行不超過 4，失敗項目不會讓其他項目被當成成功", async () => {
    let inflight = 0;
    let maxInflight = 0;
    const seen = [];
    const result = await mapLimited([1, 2, 3, 4, 5, 6], async (id) => {
      inflight += 1;
      maxInflight = Math.max(maxInflight, inflight);
      await new Promise((resolve) => setTimeout(resolve, 20));
      inflight -= 1;
      seen.push(id);
      if (id === 2 || id === 5) throw new Error(`fail ${id}`);
    }, BULK_CONCURRENCY);
    expect(BULK_CONCURRENCY).toBe(4);
    expect(maxInflight).toBeLessThanOrEqual(4);
    expect(maxInflight).toBeGreaterThan(1);
    expect(result.succeeded).toEqual([1, 3, 4, 6]);
    expect(result.failed.map((row) => row.item)).toEqual([2, 5]);
    expect(seen.slice().sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("摘要 id 去重、丟掉非整數，每批最多 200，批次依序且短計數整批算失敗", async () => {
    expect(SUMMARY_BULK_MAX).toBe(200);
    const ids = [];
    for (let n = 1; n <= 201; n += 1) {
      ids.push(n);
      if (n === 1) ids.push(1, 0, -3, 1.5, true);
    }
    const chunks = chunkPositiveIds(ids, SUMMARY_BULK_MAX);
    expect(chunks).toHaveLength(2);
    expect(chunks[0]).toEqual(Array.from({ length: 200 }, (_v, i) => i + 1));
    expect(chunks[1]).toEqual([201]);
    expect(chunkPositiveIds(["2", 2, 0, -1, 1.2])).toEqual([[2]]);

    const calls = [];
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    const pending = deleteSummaryBatches([1, 2], async (chunk) => {
      calls.push(chunk);
      if (calls.length === 1) await gate;
      return { deleted: chunk.length - 1 };
    }, 1);
    await vi.waitFor(() => expect(calls).toEqual([[1]]));
    await new Promise((r) => setTimeout(r, 15));
    expect(calls).toEqual([[1]]);
    release();
    const result = await pending;
    expect(calls).toEqual([[1], [2]]);
    expect(result.succeeded).toEqual([]);
    expect(result.failed).toEqual([1, 2]);
  });

  it("移入目標只有自訂群組與取消群組，遠端命中不會漏掉本地沒有的列", () => {
    const options = moveFolderOptions([
      { id: "all", name: "全部" },
      { id: "starred", name: "已加星" },
      { id: "proj", name: "專案" },
    ]);
    expect(options.map((o) => o.value)).toEqual(["proj", "all"]);
    expect(options.map((o) => o.label)).toEqual(["專案", "取消群組"]);
    expect(options.some((o) => o.value === "starred" || o.label === "已加星")).toBe(false);

    const extras = appendRemoteHits(
      [{ id: 1, title: "本地" }],
      [{ id: 1, title: "本地" }],
      [
        { id: 1, title: "本地也在伺服器" },
        { id: 9, title: "只在伺服器", updated_at: "2026-01-01T00:00:00Z" },
        { id: 9, title: "重複" },
      ],
    );
    expect(extras.map((row) => row.id)).toEqual([9]);
    expect(extras[0].title).toBe("只在伺服器");
    expect(extras[0].updatedAt).toBe("2026-01-01T00:00:00Z");
  });

  it("由 messagesByConv 推出正在串流的對話 id", () => {
    const ids = streamingConversationIds({
      55: [{ role: "assistant", streaming: true }],
      66: [{ role: "assistant", streaming: false }],
      77: [{ role: "user" }],
    });
    expect(ids.has(55)).toBe(true);
    expect(ids.has(66)).toBe(false);
    expect(ids.has(77)).toBe(false);
  });

  it("bindExpectedUser 併入既有 headers 與 body，非法 id 不呼叫 authRequest", async () => {
    const auth = vi.fn(async () => ({ deleted: 1 }));
    const bound = bindExpectedUser(auth, 12);
    await bound("/api/memory/summaries/bulk-delete", {
      method: "POST",
      headers: { "X-Trace": "keep" },
      body: JSON.stringify({ ids: [3] }),
    });
    expect(auth).toHaveBeenCalledTimes(1);
    expect(auth).toHaveBeenCalledWith("/api/memory/summaries/bulk-delete", {
      method: "POST",
      headers: { "X-Trace": "keep", "X-ANILA-Expected-User-ID": "12" },
      body: JSON.stringify({ ids: [3] }),
    });

    const blocked = bindExpectedUser(auth, 0);
    await expect(blocked("/api/memory/summaries", {
      method: "DELETE",
      headers: { "X-Trace": "keep" },
    })).rejects.toMatchObject({ code: "expected-user-missing" });
    await expect(bindExpectedUser(auth, 1.5)("/api/conversations/1", {
      method: "PUT",
      body: JSON.stringify({ folder: "proj" }),
    })).rejects.toMatchObject({ code: "expected-user-missing" });
    await expect(bindExpectedUser(auth, "1")("/api/conversations/1", {
      method: "DELETE",
    })).rejects.toMatchObject({ code: "expected-user-missing" });
    expect(auth).toHaveBeenCalledTimes(1);
  });

  it("綁定後仍由 authRequest 附上 CSRF，不覆蓋既有 header", async () => {
    document.cookie = "anila_csrf=csrf-keep; path=/";
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: true }),
      text: async () => "",
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const bound = bindExpectedUser(authRequest, 4);
      await bound("/api/conversations/9", {
        method: "PUT",
        headers: { "X-Trace": "keep" },
        body: JSON.stringify({ folder: "proj" }),
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const init = fetchMock.mock.calls[0][1];
      expect(init.method).toBe("PUT");
      expect(JSON.parse(init.body)).toEqual({ folder: "proj" });
      expect(init.headers["X-ANILA-Expected-User-ID"]).toBe("4");
      expect(init.headers["X-CSRF-Token"]).toBe("csrf-keep");
      expect(init.headers["X-Trace"]).toBe("keep");
      expect(init.headers["Content-Type"]).toBe("application/json");
    } finally {
      vi.unstubAllGlobals();
      document.cookie = "anila_csrf=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
    }
  });
});
