// 涉密安全:對話 origin scope 與「開啟前必須先 hydrate」的判準。
//
// 為什麼這幾條是安全測試而不是 UI 測試:classified / classification_level
// **只存在於 conversation 物件上**。搜尋(命令面板、側欄)可以回傳本地清單
// 沒有的對話;只拿到 id 就渲染訊息 → selectedConv=null → isClassified=false
// → 浮水印消失、複製 / 編輯 / prompt-action 的機密限制全開。
import { describe, it, expect, vi } from "vitest";

import {
  ANILALM_ORIGIN,
  filterShellScopedRows,
  isConversationHydrated,
  isShellScopedConversation,
  isShellScopedOrigin,
  mergeServerConversations,
  renderableMessages,
  resolveConversationOpen,
} from "../runtime/convScope.js";

describe("origin scope", () => {
  it("放行 legacy NULL / 空字串 origin(與 exclude_origin=anilalm 清單一致)", () => {
    expect(isShellScopedOrigin(null)).toBe(true);
    expect(isShellScopedOrigin(undefined)).toBe(true);
    expect(isShellScopedOrigin("")).toBe(true);
    expect(isShellScopedOrigin("anila-ui")).toBe(true);
  });

  it("擋下 ANILALM 的對話", () => {
    expect(isShellScopedOrigin(ANILALM_ORIGIN)).toBe(false);
    expect(isShellScopedConversation({ id: 1, origin: ANILALM_ORIGIN })).toBe(false);
    expect(isShellScopedConversation(null)).toBe(false);
  });

  it("filterShellScopedRows 把跨 origin 的搜尋結果剔除", () => {
    const rows = [
      { id: 1, title: "本 app", origin: "anila-ui" },
      { id: 2, title: "legacy", origin: null },
      { id: 3, title: "知識庫的機密對話", origin: ANILALM_ORIGIN, classified: true },
    ];
    expect(filterShellScopedRows(rows).map((r) => r.id)).toEqual([1, 2]);
  });

  it("非陣列輸入回空陣列(不炸)", () => {
    expect(filterShellScopedRows(null)).toEqual([]);
    expect(filterShellScopedRows(undefined)).toEqual([]);
  });
});

describe("渲染閘門 —— 只有 ID、沒有 conversation 時不得渲染訊息", () => {
  const CLASSIFIED_MSGS = {
    42: [{ id: "m1", role: "assistant", text: "機密內文不可外洩" }],
  };

  it("selectedConvId 有值但查不到 conversation → 未 hydrate", () => {
    expect(isConversationHydrated(42, null)).toBe(false);
    expect(isConversationHydrated(42, undefined)).toBe(false);
  });

  it("沒選任何對話 → 視為已 hydrate(新對話畫面本來就沒有訊息)", () => {
    expect(isConversationHydrated(null, null)).toBe(true);
    expect(isConversationHydrated(undefined, null)).toBe(true);
  });

  it("**核心斷言**:只有 ID、無本地 conversation → 一則訊息都不回,"
    + "所以 classified 對話不可能以未分類狀態渲染", () => {
    // 訊息其實已經在 messagesByConv 裡(例如舊路徑先抓了訊息才補分類)。
    expect(CLASSIFIED_MSGS[42]).toHaveLength(1);
    // 但 conversation 物件不在 → 渲染層拿到的是空陣列。
    expect(renderableMessages(42, null, CLASSIFIED_MSGS)).toEqual([]);
  });

  it("conversation 到手後才把訊息交出去(且帶著 classified 旗標)", () => {
    const conv = { id: 42, title: "機密案", classified: true, classificationLevel: "極機密" };
    expect(renderableMessages(42, conv, CLASSIFIED_MSGS)).toHaveLength(1);
    expect(Boolean(conv.classified)).toBe(true);
  });

  it("沒選對話時回空陣列", () => {
    expect(renderableMessages(null, null, CLASSIFIED_MSGS)).toEqual([]);
  });
});

describe("resolveConversationOpen", () => {
  it("本地已有 → 直接 ready,不打後端", async () => {
    const fetchDetail = vi.fn();
    const local = { id: 7, title: "本地", classified: true };
    const result = await resolveConversationOpen({
      convId: 7,
      localConversations: [local],
      fetchDetail,
    });
    expect(result).toMatchObject({ status: "ready", fromLocal: true });
    expect(result.conversation).toBe(local);
    expect(fetchDetail).not.toHaveBeenCalled();
  });

  it("本地沒有 → 先抓完整 conversation(含 classification)再放行", async () => {
    const detail = {
      id: 42,
      title: "機密案",
      origin: "anila-ui",
      classified: true,
      classification_level: "極機密",
      messages: [{ id: 1, role: "assistant", content: "機密內文" }],
    };
    const fetchDetail = vi.fn(async () => detail);
    const result = await resolveConversationOpen({
      convId: 42,
      localConversations: [],
      fetchDetail,
    });
    expect(fetchDetail).toHaveBeenCalledWith(42);
    expect(result.status).toBe("ready");
    expect(result.fromLocal).toBe(false);
    expect(result.detail.classified).toBe(true);
    expect(result.detail.classification_level).toBe("極機密");
  });

  it("跨 origin(ANILALM)→ denied,呼叫端必須拒絕開啟", async () => {
    const fetchDetail = vi.fn(async () => ({
      id: 99,
      title: "知識庫對話",
      origin: ANILALM_ORIGIN,
      classified: true,
      messages: [{ id: 1, role: "assistant", content: "不該出現" }],
    }));
    const result = await resolveConversationOpen({
      convId: 99,
      localConversations: [],
      fetchDetail,
    });
    expect(result).toEqual({ status: "denied", reason: "origin" });
    expect(result.detail).toBeUndefined();
  });

  it("後端錯誤 → error(不是靜默把 id 選下去)", async () => {
    const boom = new Error("500");
    const result = await resolveConversationOpen({
      convId: 42,
      localConversations: [],
      fetchDetail: async () => { throw boom; },
    });
    expect(result).toEqual({ status: "error", error: boom });
  });

  it("拿不到 detail / 沒有 fetcher → denied", async () => {
    await expect(
      resolveConversationOpen({ convId: 42, localConversations: [] }),
    ).resolves.toEqual({ status: "denied", reason: "unresolvable" });
    await expect(
      resolveConversationOpen({ convId: 42, localConversations: [], fetchDetail: async () => null }),
    ).resolves.toEqual({ status: "denied", reason: "unresolvable" });
    await expect(
      resolveConversationOpen({ convId: null, localConversations: [] }),
    ).resolves.toEqual({ status: "denied", reason: "unresolvable" });
  });
});

// 較晚抵達的初始清單不可以把「後端還不知道」的離線本地列一起抹掉 ——
// 抹掉之後 selectedConvId 就成了孤兒,渲染閘門會停在「對話載入中…」。
describe("mergeServerConversations", () => {
  it("保留伺服器快照裡不可能存在的離線本地列(cv-local-*)", () => {
    const prev = [
      { id: "cv-local-1", title: "離線建立的" },
      { id: 7, title: "本地舊值" },
    ];
    const incoming = [{ id: 7, title: "伺服器值" }];
    const merged = mergeServerConversations(prev, incoming);
    expect(merged.map((c) => c.id)).toEqual(["cv-local-1", 7]);
    // 伺服器認得的 id 一律以伺服器為準(不讓本地舊值蓋掉 classification)。
    expect(merged.find((c) => c.id === 7).title).toBe("伺服器值");
  });

  it("本地的數字 id 幽靈列不會被復活(別的分頁刪掉的對話)", () => {
    const merged = mergeServerConversations(
      [{ id: 7, title: "已被別的分頁刪掉" }],
      [{ id: 8, title: "伺服器唯一的一則" }],
    );
    expect(merged.map((c) => c.id)).toEqual([8]);
  });

  it("沒有離線本地列時直接沿用伺服器清單", () => {
    const incoming = [{ id: 1 }, { id: 2 }];
    expect(mergeServerConversations([], incoming)).toBe(incoming);
    expect(mergeServerConversations(null, incoming)).toBe(incoming);
    expect(mergeServerConversations([{ id: "cv-local-1" }], null)).toEqual([
      { id: "cv-local-1" },
    ]);
  });

  it("離線本地列若已被伺服器認領(同 id)就不重複保留", () => {
    const merged = mergeServerConversations(
      [{ id: "cv-local-1", title: "離線" }],
      [{ id: "cv-local-1", title: "伺服器也回了同一把 key" }],
    );
    expect(merged).toHaveLength(1);
    expect(merged[0].title).toBe("伺服器也回了同一把 key");
  });
});
