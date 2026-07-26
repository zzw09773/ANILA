// W2-4 ①② —— 串流持久化的順序契約。
//
// 缺陷本體:兩則 append 都排在串流**成功之後**,失敗路徑再用 `text:` 覆蓋
// 已生成的文字 —— 網路一斷,使用者眼前累積的回答連同那則 user 訊息一起消失。
//
// 本檔用 fake 持久層(記錄每一次寫入)當「mock 持久層」,重整後看得到什麼
// 就等於 fake 裡留下什麼 rows。斷言:
//   ① user row 在任何 assistant 寫入**之前**就落地;
//   ② 失敗時累積文字 + error metadata 落地(而不是被錯誤字串覆蓋);
//   ③ checkpoint 節流 ≥2s 且僅在有 delta 時才寫(別打資料庫);
//   ④ checkpoint 建過 row 之後,終局走 update 不再 append(不長出孤兒列)。

import { describe, it, expect, vi } from "vitest";
import {
  CHECKPOINT_MIN_INTERVAL_MS,
  createTurnPersistence,
} from "../runtime/streamPersistence.js";

// 極小的 fake 持久層:rows 就是「重整後讀回來的東西」。
function fakeBackend() {
  const rows = [];
  const calls = [];
  let nextId = 100;
  return {
    rows,
    calls,
    append: vi.fn(async (payload) => {
      calls.push({ op: "append", payload });
      const row = { id: nextId++, ...payload };
      rows.push(row);
      return row;
    }),
    update: vi.fn(async (dbId, patch) => {
      calls.push({ op: "update", dbId, patch });
      const row = rows.find((r) => r.id === dbId);
      if (!row) throw new Error(`row ${dbId} not found`);
      Object.assign(row, patch);
      return row;
    }),
  };
}

function makeClock(start = 0) {
  let t = start;
  return { now: () => t, advance: (ms) => { t += ms; } };
}

function persistence(backend, clock, overrides = {}) {
  return createTurnPersistence({
    appendMessage: backend.append,
    updateMessage: backend.update,
    now: clock.now,
    ...overrides,
  });
}

describe("① user 訊息在串流開始前持久化", () => {
  it("persistUser 落地的 row 排在所有 assistant 寫入之前", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);

    await p.persistUser({ content: "使用者問句" });
    // 串流開始 → 累積 → 終局
    clock.advance(CHECKPOINT_MIN_INTERVAL_MS + 1);
    await p.checkpoint("半成品");
    await p.finalizeAssistant({ content: "完整回答" });

    const roles = backend.rows.map((r) => r.role);
    expect(roles[0]).toBe("user");
    expect(roles).toContain("assistant");
    expect(roles.indexOf("user")).toBeLessThan(roles.indexOf("assistant"));
  });

  it("persistUser 回傳的 dbId 透過 onUserSaved 回報", async () => {
    const backend = fakeBackend();
    const onUserSaved = vi.fn();
    const p = persistence(backend, makeClock(), { onUserSaved });
    await p.persistUser({ content: "hi" });
    expect(onUserSaved).toHaveBeenCalledWith(backend.rows[0].id);
    expect(p.userDbId).toBe(backend.rows[0].id);
  });

  it("persistUser 失敗只回報錯誤,不阻斷串流(不 throw)", async () => {
    const backend = fakeBackend();
    backend.append.mockRejectedValueOnce(new Error("boom"));
    const onError = vi.fn();
    const p = persistence(backend, makeClock(), { onError });
    await expect(p.persistUser({ content: "hi" })).resolves.toBeNull();
    expect(onError).toHaveBeenCalled();
  });

  it("enabled=false(本地未落後端的暫時對話)完全不寫", async () => {
    const backend = fakeBackend();
    const p = persistence(backend, makeClock(), { enabled: false });
    await p.persistUser({ content: "hi" });
    await p.finalizeAssistant({ content: "x" });
    expect(backend.append).not.toHaveBeenCalled();
    expect(backend.update).not.toHaveBeenCalled();
  });
});

describe("③ checkpoint 節流 ≥2s 且僅 delta", () => {
  it("未滿 2s 不寫", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);
    await p.persistUser({ content: "q" });
    backend.append.mockClear();

    clock.advance(CHECKPOINT_MIN_INTERVAL_MS - 1);
    await p.checkpoint("累積中");
    expect(backend.append).not.toHaveBeenCalled();
  });

  it("滿 2s 才寫一次;同一段文字再叫不會重複寫(僅 delta)", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);

    clock.advance(CHECKPOINT_MIN_INTERVAL_MS);
    await p.checkpoint("第一段");
    expect(backend.append).toHaveBeenCalledTimes(1);

    clock.advance(CHECKPOINT_MIN_INTERVAL_MS);
    await p.checkpoint("第一段"); // 無 delta
    expect(backend.append).toHaveBeenCalledTimes(1);
    expect(backend.update).not.toHaveBeenCalled();

    clock.advance(CHECKPOINT_MIN_INTERVAL_MS);
    await p.checkpoint("第一段第二段"); // 有 delta → update 既有 row
    expect(backend.update).toHaveBeenCalledTimes(1);
  });

  it("空字串不建 row(還沒有任何 token 值得保留)", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);
    clock.advance(CHECKPOINT_MIN_INTERVAL_MS * 5);
    await p.checkpoint("");
    expect(backend.append).not.toHaveBeenCalled();
  });

  it("大量 token 只換算成少數幾次寫入", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);
    let acc = "";
    // 600 個 token,每 10ms 一個 → 6 秒 → 至多 3 次寫入
    for (let i = 0; i < 600; i++) {
      acc += "字";
      clock.advance(10);
      await p.checkpoint(acc);
    }
    const writes = backend.append.mock.calls.length + backend.update.mock.calls.length;
    expect(writes).toBeLessThanOrEqual(3);
    expect(writes).toBeGreaterThan(0);
  });
});

describe("② 失敗路徑保留累積文字 + error metadata", () => {
  it("mid-stream 失敗:partial 文字落地,metadata 帶 error,內容不是錯誤字串", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);

    await p.persistUser({ content: "使用者問句" });
    // 才 300ms 就斷線 —— 節流還沒到,但失敗必須強制寫。
    clock.advance(300);
    await p.failAssistant({
      content: "已經生成的一半回答",
      error: { code: "SERVICE_UNAVAILABLE", message: "上游服務中斷，請稍後重試。" },
    });

    const assistant = backend.rows.find((r) => r.role === "assistant");
    expect(assistant).toBeTruthy();
    expect(assistant.content).toBe("已經生成的一半回答");
    expect(assistant.content).not.toContain("請求失敗");
    expect(assistant.metadata.error.code).toBe("SERVICE_UNAVAILABLE");
    expect(assistant.metadata.partial).toBe(true);

    // 「重整後」= 讀回 rows:user + partial 都在。
    expect(backend.rows.map((r) => r.role)).toEqual(["user", "assistant"]);
    expect(backend.rows[0].content).toBe("使用者問句");
  });

  it("失敗且完全沒有累積文字 → 不寫空的 assistant row", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);
    await p.persistUser({ content: "q" });
    backend.append.mockClear();
    await p.failAssistant({ content: "", error: { code: "UNAUTHENTICATED", message: "x" } });
    expect(backend.append).not.toHaveBeenCalled();
  });

  it("checkpoint 已建 row → 失敗走 update,不長出第二列", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);
    clock.advance(CHECKPOINT_MIN_INTERVAL_MS);
    await p.checkpoint("一半");
    await p.failAssistant({ content: "一半又多一點", error: { code: "UPSTREAM_TIMEOUT", message: "x" } });

    expect(backend.rows.filter((r) => r.role === "assistant")).toHaveLength(1);
    expect(backend.rows.find((r) => r.role === "assistant").content).toBe("一半又多一點");
  });
});

describe("④ 終局不長出孤兒列", () => {
  it("checkpoint 建過 row → finalizeAssistant 走 update", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);
    clock.advance(CHECKPOINT_MIN_INTERVAL_MS);
    await p.checkpoint("一半");
    expect(backend.append).toHaveBeenCalledTimes(1);

    await p.finalizeAssistant({ content: "完整回答", traceId: "t-1", latencyMs: 42 });
    expect(backend.append).toHaveBeenCalledTimes(1);
    expect(backend.update).toHaveBeenCalledTimes(1);
    expect(backend.rows.filter((r) => r.role === "assistant")).toHaveLength(1);
    expect(backend.rows.find((r) => r.role === "assistant").content).toBe("完整回答");
  });

  it("沒 checkpoint 過(短回答)→ finalizeAssistant append 一次,並回報 dbId", async () => {
    const backend = fakeBackend();
    const onAssistantSaved = vi.fn();
    const p = persistence(backend, makeClock(), { onAssistantSaved });
    await p.finalizeAssistant({ content: "短答" });
    expect(backend.append).toHaveBeenCalledTimes(1);
    expect(onAssistantSaved).toHaveBeenCalledWith(backend.rows[0].id);
  });

  it("兩個 checkpoint 同時觸發也只建一列(寫入序列化)", async () => {
    const backend = fakeBackend();
    const clock = makeClock();
    const p = persistence(backend, clock);
    clock.advance(CHECKPOINT_MIN_INTERVAL_MS);
    await Promise.all([p.checkpoint("甲"), p.checkpoint("甲乙")]);
    expect(backend.rows.filter((r) => r.role === "assistant")).toHaveLength(1);
  });

  it("既有 dbId(regenerate 情境)可預先注入 → 一律走 update", async () => {
    const backend = fakeBackend();
    backend.rows.push({ id: 7, role: "assistant", content: "舊答" });
    const p = persistence(backend, makeClock(), { assistantDbId: 7 });
    await p.finalizeAssistant({ content: "新答" });
    expect(backend.append).not.toHaveBeenCalled();
    expect(backend.update).toHaveBeenCalledWith(7, expect.objectContaining({ content: "新答" }));
  });
});
