// Duplicate assistant after edit-resend / regenerate.
// Contract: persist/branch 2xx body → reconcilePersistedAssistant → backfill
// patch on the optimistic bubble BEFORE refreshActivePath. Without dbId,
// applyServerPath keeps the pending local as a hydrate tail → two assistants.
//
// Strength: semantic mutation of the numeric-id guard inside
// reconcilePersistedAssistant must turn these tests red (both paths).

// @source-text-guard — 本檔含「讀原始碼 + toContain」的字串比對測試。
// 這類斷言只證明某段文字還在檔案裡,**不證明它在執行時會發生**:
// 呼叫端整個被繞過、狀態沒接上、時序錯了,它照樣綠。
// 它們擋的是「整段被刪掉」,不能當成行為覆蓋率。
// 對應的行為測試在 src/__tests__/orchestrator*.test.jsx。

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

  it("handleEditUser uses reconcile + surfaces !ok with persistError", () => {
    expect(editBody).toMatch(/reconcilePersistedAssistant\s*\(/);
    // Load-bearing line: the patch must actually be APPLIED — mutation
    // dropping this is the original defect itself (dup bubble until F5).
    expect(editBody).toMatch(
      /updateMsg\(\s*convId\s*,\s*assistantId\s*,\s*reconciled\.patch\s*\)/,
    );
    // Branch polarity: success applies, failure surfaces.
    expect(editBody).toMatch(/if \(reconciled\.ok\) \{/);
    expect(editBody).toMatch(/persistError:\s*reconciled\.notice/);
    expect(editBody).toMatch(/setRuntimeError\s*\(\s*reconciled\.error/);
    // Backfill must land BEFORE the server reconciliation reads the list.
    const backfillIdx = editBody.indexOf("reconciled.patch");
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
