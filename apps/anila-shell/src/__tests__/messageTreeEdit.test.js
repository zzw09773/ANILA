import { describe, it, expect, vi } from "vitest";
import {
  buildEditRerunMessages,
  buildMessageHistory,
} from "../runtime/messageHistory.js";
import {
  activePathFromTree,
  applyRevisionSwitch,
  attachRevisionsFromSiblings,
  deepestLeafUnder,
  descendantTailFromTree,
  siblingsOf,
} from "../runtime/messageTree.js";
import {
  editUserMessage,
  forkAssistantMessage,
  setActiveLeaf,
} from "../runtime/conversations.js";

function identityContent(text) {
  return text;
}

describe("buildEditRerunMessages (W2-3 A3)", () => {
  it("re-run payload contains the full active-path history, not one sentence", () => {
    const path = [
      { role: "user", text: "first question" },
      { role: "assistant", text: "first answer" },
      { role: "user", text: "second question" },
      { role: "assistant", text: "second answer" },
      { role: "user", text: "third question" },
    ];
    const payload = buildEditRerunMessages(
      path,
      2, // edit the second user turn
      "second question edited",
      [],
      identityContent,
    );
    expect(payload).toEqual([
      { role: "user", content: "first question" },
      { role: "assistant", content: "first answer" },
      { role: "user", content: "second question edited" },
    ]);
    expect(payload).toHaveLength(3);
    expect(payload.map((m) => m.content).join("|")).not.toBe("second question edited");
  });

  it("buildMessageHistory keeps prior turns", () => {
    const out = buildMessageHistory(
      [
        { role: "user", text: "a" },
        { role: "assistant", text: "b" },
      ],
      "c",
      [],
      identityContent,
    );
    expect(out).toHaveLength(3);
  });
});

describe("sibling version nav (W2-3 A4)", () => {
  const tree = [
    { id: 1, dbId: 1, role: "user", text: "q", parentId: null, createdAt: "2026-07-27T00:00:01Z" },
    { id: 2, dbId: 2, role: "assistant", text: "a1", parentId: 1, createdAt: "2026-07-27T00:00:02Z" },
    { id: 3, dbId: 3, role: "assistant", text: "a2", parentId: 1, createdAt: "2026-07-27T00:00:03Z" },
    { id: 4, dbId: 4, role: "user", text: "follow", parentId: 3, createdAt: "2026-07-27T00:00:04Z" },
  ];

  it("N/M equals sibling-set size", () => {
    const path = activePathFromTree(tree, 3);
    expect(path.map((m) => m.dbId)).toEqual([1, 3]);
    const withRevs = attachRevisionsFromSiblings(path, tree);
    const assistant = withRevs.find((m) => m.role === "assistant");
    expect(assistant.revisions).toHaveLength(2);
    expect(assistant.activeRev).toBe(1); // a2 is the active leaf's ancestor
    // UI shows (activeRev + 1) / total → 2 / 2
    expect(`${assistant.activeRev + 1} / ${assistant.revisions.length}`).toBe("2 / 2");
  });

  it("switching active leaf updates the rendered path", () => {
    const pathA2 = activePathFromTree(tree, 3);
    expect(pathA2.map((m) => m.text)).toEqual(["q", "a2"]);
    const pathA1 = activePathFromTree(tree, 2);
    expect(pathA1.map((m) => m.text)).toEqual(["q", "a1"]);
    const sibs = siblingsOf(tree, tree[1]);
    expect(sibs).toHaveLength(2);
  });
});

describe("two-level branch switch keeps continuation (W2-3)", () => {
  // q → a1 → follow → reply
  //    ↘ a2
  const tree = [
    { id: 1, dbId: 1, role: "user", text: "q", parentId: null, createdAt: "2026-07-27T00:00:01Z" },
    { id: 2, dbId: 2, role: "assistant", text: "a1", parentId: 1, createdAt: "2026-07-27T00:00:02Z" },
    { id: 3, dbId: 3, role: "assistant", text: "a2", parentId: 1, createdAt: "2026-07-27T00:00:03Z" },
    { id: 4, dbId: 4, role: "user", text: "follow", parentId: 2, createdAt: "2026-07-27T00:00:04Z" },
    { id: 5, dbId: 5, role: "assistant", text: "reply", parentId: 4, createdAt: "2026-07-27T00:00:05Z" },
  ];

  it("revision under a1 carries the descendant tail; switch restores it", () => {
    expect(deepestLeafUnder(tree, 2)).toBe(5);
    expect(descendantTailFromTree(tree, 2).map((m) => m.dbId)).toEqual([4, 5]);
    expect(descendantTailFromTree(tree, 3)).toEqual([]);

    const path = activePathFromTree(tree, 5);
    const withRevs = attachRevisionsFromSiblings(path, tree);
    const assistant = withRevs.find((m) => m.role === "assistant");
    expect(assistant.dbId).toBe(2);
    expect(assistant.revisions[0].tail.map((m) => m.dbId)).toEqual([4, 5]);
    expect(assistant.revisions[1].tail).toEqual([]);

    const list = withRevs; // [q, a1(+revs), follow, reply] — wait, path is full
    // active path is [q, a1, follow, reply]; attach only mutates path nodes
    expect(list.map((m) => m.text)).toEqual(["q", "a1", "follow", "reply"]);

    const toA2 = applyRevisionSwitch(list, assistant, 1);
    expect(toA2.nextList.map((m) => m.text)).toEqual(["q", "a2"]);
    expect(toA2.activeLeafId).toBe(3);

    const a2Msg = toA2.nextList[1];
    const backToA1 = applyRevisionSwitch(toA2.nextList, a2Msg, 0);
    expect(backToA1.nextList.map((m) => m.text)).toEqual([
      "q",
      "a1",
      "follow",
      "reply",
    ]);
    expect(backToA1.activeLeafId).toBe(5);
  });
});

describe("mutation client calls (W2-3)", () => {
  it("setActiveLeaf PUTs message_id", async () => {
    const authRequest = vi.fn(async () => ({ id: 1 }));
    await setActiveLeaf(authRequest, 42, 99);
    expect(authRequest).toHaveBeenCalledWith("/api/conversations/42/active-leaf", {
      method: "PUT",
      body: JSON.stringify({ message_id: 99 }),
    });
  });

  it("forkAssistantMessage POSTs sibling content", async () => {
    const authRequest = vi.fn(async () => ({ id: 2 }));
    await forkAssistantMessage(authRequest, 7, 11, { content: "regen" });
    expect(authRequest).toHaveBeenCalledWith(
      "/api/conversations/7/messages/11/fork",
      {
        method: "POST",
        body: JSON.stringify({
          content: "regen",
          trace_id: null,
          latency_ms: null,
          model_name: null,
          agent_name: null,
          metadata: null,
        }),
      },
    );
  });

  it("editUserMessage PUTs edited content", async () => {
    const authRequest = vi.fn(async () => ({ id: 3 }));
    await editUserMessage(authRequest, 5, 8, "edited");
    expect(authRequest).toHaveBeenCalledWith(
      "/api/conversations/5/messages/8/edit",
      {
        method: "PUT",
        body: JSON.stringify({ content: "edited" }),
      },
    );
  });

  it("applyRevisionSwitch reports deepest leaf for persistence", () => {
    const tree = [
      { id: 1, dbId: 1, role: "user", text: "q", parentId: null, createdAt: "t1" },
      { id: 2, dbId: 2, role: "assistant", text: "a1", parentId: 1, createdAt: "t2" },
      { id: 3, dbId: 3, role: "assistant", text: "a2", parentId: 1, createdAt: "t3" },
      { id: 4, dbId: 4, role: "user", text: "follow", parentId: 2, createdAt: "t4" },
    ];
    const path = attachRevisionsFromSiblings(activePathFromTree(tree, 4), tree);
    const assistant = path.find((m) => m.role === "assistant");
    const { activeLeafId } = applyRevisionSwitch(path, assistant, 1);
    expect(activeLeafId).toBe(3);
    const back = applyRevisionSwitch(
      applyRevisionSwitch(path, assistant, 1).nextList,
      applyRevisionSwitch(path, assistant, 1).nextList[1],
      0,
    );
    expect(back.activeLeafId).toBe(4);
  });

  it("applyRevisionSwitch returns a null leaf for a revision that was never persisted", () => {
    // 本地 regenerate / edit 產生的版本在 fork/edit 回來之前沒有 dbId。
    // 這時伺服器端沒有任何葉可以指 —— 必須回傳 null,呼叫端才知道要跳過 PUT。
    const assistant = {
      id: "local-a",
      role: "assistant",
      text: "答案一",
      dbId: 77,
      parentId: 1,
      activeRev: 0,
      revisions: [
        { text: "答案一", dbId: 77, tail: [] },
        { text: "重生中的答案", tail: [] }, // fork 還沒回來:沒有 dbId
      ],
    };
    const list = [{ id: "local-u", role: "user", text: "問題", dbId: 1 }, assistant];

    const { nextList, activeLeafId } = applyRevisionSwitch(list, assistant, 1);
    expect(nextList[1].text).toBe("重生中的答案");
    // ⚠ 迴歸點:舊實作從合併後的 assistant 讀 dbId,而那一欄會回退成**前一個
    // 兄弟**的 77 —— 等於把伺服器的 active leaf 指回使用者剛離開的那一枝。
    expect(activeLeafId).toBeNull();

    // `handleEditUser` 明寫 dbId: null,同樣要回 null。
    const withNull = {
      ...assistant,
      revisions: [assistant.revisions[0], { text: "編輯後", dbId: null, tail: [] }],
    };
    expect(applyRevisionSwitch(list, withNull, 1).activeLeafId).toBeNull();
  });
});
