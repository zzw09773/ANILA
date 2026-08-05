// Duplicate assistant after edit-resend / regenerate.
// Contract: persist/branch 2xx body → reconcilePersistedAssistant → backfill
// patch on the optimistic bubble BEFORE refreshActivePath. Without dbId,
// applyServerPath keeps the pending local as a hydrate tail → two assistants.
//
// Strength: semantic mutation of the numeric-id guard inside
// reconcilePersistedAssistant must turn these tests red (both paths).

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  applyServerPath,
  reconcilePersistedAssistant,
} from "../runtime/messageTree.js";

const appSrc = readFileSync(resolve(process.cwd(), "src/app.jsx"), "utf8");

function functionBody(source, name) {
  const start = source.indexOf(`function ${name}`);
  expect(start, `${name} not found`).toBeGreaterThanOrEqual(0);
  let depth = 0;
  let began = false;
  for (let i = start; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") {
      depth += 1;
      began = true;
    } else if (ch === "}") {
      depth -= 1;
      if (began && depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

const mapServerMessage = (m) => ({
  id: `srv-${m.id}`,
  dbId: m.id,
  role: m.role,
  text: m.content,
  parentId: m.parent_id ?? null,
  siblingIndex: m.sibling_index ?? 0,
  siblingCount: m.sibling_count ?? 1,
  siblingIds: m.sibling_ids ?? [m.id],
  reasoning: m.reasoning ?? null,
  streaming: false,
});

describe("reconcilePersistedAssistant (persist result → backfill patch)", () => {
  it("saved = null → failure with user-visible notice", () => {
    const r = reconcilePersistedAssistant(null, 112);
    expect(r.ok).toBe(false);
    expect(r.patch).toBeNull();
    expect(r.error).toBeInstanceOf(Error);
    expect(r.error.message).toBe("對話訊息儲存失敗");
    expect(r.notice).toContain("沒有存進對話紀錄");
    expect(r.notice).toContain("對話訊息儲存失敗");
  });

  it("saved = {} → failure (2xx-without-id)", () => {
    const r = reconcilePersistedAssistant({}, 112);
    expect(r.ok).toBe(false);
    expect(r.patch).toBeNull();
    expect(r.notice).toContain("沒有存進對話紀錄");
  });

  it('saved = {id:"x"} → failure (non-numeric id)', () => {
    const r = reconcilePersistedAssistant({ id: "x" }, 112);
    expect(r.ok).toBe(false);
    expect(r.patch).toBeNull();
    expect(r.notice).toContain("沒有存進對話紀錄");
  });

  it("valid saved → field-complete patch; parent_id wins over fallback", () => {
    const saved = {
      id: 113,
      parent_id: 112,
      sibling_index: 0,
      sibling_count: 1,
      sibling_ids: [113],
    };
    const r = reconcilePersistedAssistant(saved, 999);
    expect(r.ok).toBe(true);
    expect(r.patch).toEqual({
      dbId: 113,
      parentId: 112,
      siblingIndex: 0,
      siblingCount: 1,
      siblingIds: [113],
      persistError: null,
    });
    expect(r.activeLeafMessageId).toBe(113);
    expect(r.notice).toBeNull();
  });

  it("fallbackParentId used only when parent_id absent; regen passes null to avoid poisoning", () => {
    const r = reconcilePersistedAssistant({ id: 52 }, 50);
    expect(r.ok).toBe(true);
    // fallbackParentId is used only when parent_id absent; callers pass null
    // on regenerate so consecutive assistants cannot poison parentId.
    expect(r.patch.parentId).toBe(50);
    const regenShape = reconcilePersistedAssistant({ id: 52 }, null);
    expect(regenShape.patch.parentId).toBeNull();
  });
});

describe("edit-resend path: reconcile patch → applyServerPath", () => {
  const serverActivePath = [
    {
      id: 112,
      role: "user",
      content: "給我一個甜甜圈的svg",
      parent_id: null,
      sibling_index: 1,
      sibling_count: 2,
      sibling_ids: [110, 112],
    },
    {
      id: 113,
      role: "assistant",
      content: "<svg .. donut ..>",
      parent_id: 112,
      sibling_index: 0,
      sibling_count: 1,
      sibling_ids: [113],
      reasoning: "x".repeat(100),
    },
  ];
  const mapped = serverActivePath.map((m) => ({
    ...mapServerMessage(m),
    conversationId: 37,
  }));
  const saved = {
    id: 113,
    parent_id: 112,
    sibling_index: 0,
    sibling_count: 1,
    sibling_ids: [113],
  };

  it("OLD (no dbId backfill): two assistant blocks + pager still 2/2", () => {
    const localList = [
      { ...mapServerMessage(serverActivePath[0]), conversationId: 37 },
      {
        id: "a-9f3c",
        role: "assistant",
        text: "<svg .. donut ..>",
        reasoning: "x".repeat(100),
        streaming: false,
        conversationId: 37,
      },
    ];
    const rendered = applyServerPath(localList, mapped, 37);
    const assistants = rendered.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(2);
    expect(rendered[0].siblingIndex + 1).toBe(2);
    expect(rendered[0].siblingCount).toBe(2);
  });

  it("FIXED via reconcilePersistedAssistant patch: exactly one assistant", () => {
    const reconciled = reconcilePersistedAssistant(saved, 112);
    expect(reconciled.ok).toBe(true);
    const localList = [
      { ...mapServerMessage(serverActivePath[0]), conversationId: 37 },
      {
        id: "a-9f3c",
        role: "assistant",
        text: "<svg .. donut ..>",
        reasoning: "x".repeat(100),
        streaming: false,
        conversationId: 37,
        ...reconciled.patch,
      },
    ];
    const rendered = applyServerPath(localList, mapped, 37);
    const assistants = rendered.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0].dbId).toBe(113);
    expect(rendered[0].siblingIndex + 1).toBe(2);
    expect(rendered[0].siblingCount).toBe(2);
  });

  it("2xx-without-id: reconcile fails → no patch (would leave duplicate)", () => {
    const reconciled = reconcilePersistedAssistant({}, 112);
    expect(reconciled.ok).toBe(false);
    expect(reconciled.patch).toBeNull();
    expect(reconciled.notice).toContain("沒有存進對話紀錄");
  });
});

describe("regenerate path: reconcile patch → applyServerPath", () => {
  const serverActivePath = [
    {
      id: 50,
      role: "user",
      content: "解釋 RLS",
      parent_id: null,
      sibling_index: 0,
      sibling_count: 1,
      sibling_ids: [50],
    },
    {
      id: 52,
      role: "assistant",
      content: "重新產生的回答",
      parent_id: 50,
      sibling_index: 1,
      sibling_count: 2,
      sibling_ids: [51, 52],
    },
  ];
  const mapped = serverActivePath.map((m) => ({
    ...mapServerMessage(m),
    conversationId: 9,
  }));
  const saved = {
    id: 52,
    parent_id: 50,
    sibling_index: 1,
    sibling_count: 2,
    sibling_ids: [51, 52],
  };

  it("OLD (placeholder without dbId): two assistant blocks", () => {
    const localList = [
      { ...mapServerMessage(serverActivePath[0]), conversationId: 9 },
      {
        id: "a-regen",
        role: "assistant",
        text: "重新產生的回答",
        streaming: false,
        conversationId: 9,
      },
    ];
    const rendered = applyServerPath(localList, mapped, 9);
    expect(rendered.filter((m) => m.role === "assistant")).toHaveLength(2);
  });

  it("FIXED via reconcilePersistedAssistant patch: exactly one assistant", () => {
    const reconciled = reconcilePersistedAssistant(saved, null);
    expect(reconciled.ok).toBe(true);
    expect(reconciled.patch.parentId).toBe(50);
    const localList = [
      { ...mapServerMessage(serverActivePath[0]), conversationId: 9 },
      {
        id: "a-regen",
        role: "assistant",
        text: "重新產生的回答",
        streaming: false,
        conversationId: 9,
        ...reconciled.patch,
      },
    ];
    const rendered = applyServerPath(localList, mapped, 9);
    const assistants = rendered.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0].dbId).toBe(52);
  });

  it("2xx-without-id: reconcile fails with persistError notice for bubble pin", () => {
    const reconciled = reconcilePersistedAssistant({ id: "x" }, null);
    expect(reconciled.ok).toBe(false);
    expect(reconciled.notice).toContain("沒有存進對話紀錄");
    expect(reconciled.error.message).toBe("對話訊息儲存失敗");
  });
});

describe("原始碼護欄: both callers wire reconcilePersistedAssistant", () => {
  const editBody = functionBody(appSrc, "handleEditUser");
  const regenBody = functionBody(appSrc, "regenerateMessage");

  // ⚠ 2026-08-05:handleEditUser 不再「串流跑完才 append 助理訊息」——它改成
  // 先落庫再串流(POST /branch-turn 一次交易建立分支的使用者訊息 ＋ 預留的助理
  // 列),所以這條路徑上沒有 reconcilePersistedAssistant 可用。守的不變式沒有
  // 變:**樂觀氣泡必須在 refreshActivePath 讀清單之前就帶著 dbId**,否則
  // applyServerPath 會把它當成 pending tail 留著,畫面上出現兩則助理回答。
  // 新結構其實更強:dbId 在串流「開始之前」就填好了。
  // 行為面的守衛(掛載元件、真的跑一次編輯重問、數氣泡)在
  // __tests__/reservedTurn.test.jsx 的 "edit and re-ask"。
  it("handleEditUser backfills the reserved dbId before refreshActivePath", () => {
    // 助理氣泡建立時就帶著預留列的 id。
    expect(editBody).toMatch(/dbId:\s*reserved\.id/);
    // 內容寫回那一列(不是 append 一則新的)。
    expect(editBody).toMatch(/finalizeStreamedAssistant\s*\(/);
    // 失敗要浮出來,不能靜默。
    expect(editBody).toMatch(/persistError:\s*persisted\.notice/);
    expect(editBody).toMatch(/setRuntimeError\s*\(\s*persisted\.error/);
    // 順序:dbId 先落在氣泡上,refreshActivePath 才讀清單。
    const backfillIdx = editBody.indexOf("dbId: reserved.id");
    const refreshIdx = editBody.indexOf("await refreshActivePath");
    expect(backfillIdx).toBeGreaterThanOrEqual(0);
    expect(refreshIdx).toBeGreaterThan(backfillIdx);
  });

  it("regenerateMessage uses reconcile(…, null) + surfaces !ok", () => {
    expect(regenBody).toMatch(
      /reconcilePersistedAssistant\s*\(\s*savedAssistant\s*,\s*null\s*\)/,
    );
    expect(regenBody).toMatch(
      /updateMsg\(\s*convId\s*,\s*placeholderId\s*,\s*reconciled\.patch\s*\)/,
    );
    expect(regenBody).toMatch(/if \(reconciled\.ok\) \{/);
    expect(regenBody).toMatch(/persistError:\s*reconciled\.notice/);
    expect(regenBody).toMatch(/setRuntimeError\s*\(\s*reconciled\.error/);
    // Must not fall back to prevUser.dbId for parentId (poisoned by consecutive
    // assistants between user and regenerated reply).
    expect(regenBody).not.toMatch(
      /savedAssistant\.parent_id\s*\?\?\s*[\s\S]*prevUser\.dbId/,
    );
  });
});
