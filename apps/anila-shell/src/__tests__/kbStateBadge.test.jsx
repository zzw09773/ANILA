// 院內規章檢索的五個狀態,使用者看不看得見、看到的是不是同一句話。
//
// 這一包要消滅的形狀是**靜默分裂**:meta 有兩個入口(重新載入走
// `mapServerMessage`、SSE 現場走 `applyMeta`),只接一邊不會有任何錯誤訊息,
// 只會讓同一則答案在串完的當下說「有院規依據」、重新整理之後說不出話來。
// 所以兩個縫**各自**有測試:只弄壞一縫,只有那一縫的測試會紅。
//
// 第二個要消滅的形狀是**把狀態從資料反推**。生產端的事實(institutional_kb.py
// :226-233):`partial_error` **一定有命中**、`search_error` 有失敗庫而沒有命中。
// 所以這裡的 fixture 一律照真形狀給,「有命中就當 searched_hit」的反推會讓
// `partial_error` 那一輪畫出命中徽章而看不見「這不是全部的依據」——那正是
// 使用者最需要看見的一句話。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";

import { MessageBubble } from "../chat.jsx";
// ⚠ 元件層的 fixture 刻意呼叫**真的**映射函式,而不是在測試裡手抄一份
// camelCase。手抄版會跟著 production 漂開,然後這一整組測試就變成在測
// 測試自己(`dupReplyReconcile.test.js:44` 就是那個形狀)。
import { kbMetaFields } from "../app.jsx";
import {
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
  selectConversation,
  clickNewChat,
  createFakeBackend,
  screen,
  waitFor,
} from "./helpers/orchestrator.jsx";

// ---- 生產端契約(proxy.py `_kb_meta_fragment`:663-697)------------------------

const HIT_A = {
  collection_id: 7,
  document_id: 21,
  filename: "人事管理規則.pdf",
  content: "第三條 差勤一律採線上簽核，紙本不再受理。",
  score: 0.91,
};
const HIT_B = {
  collection_id: 7,
  document_id: 34,
  filename: "差勤作業要點.docx",
  content: "第五條 出差應於事前完成申請程序。",
  score: 0.78,
};

/**
 * 四個「查過了」的狀態,各自帶**真實形狀**的 payload。
 * 兩筆命中不是排場:一筆的話「只畫第一筆」「編號差一」這類壞法殺不掉。
 */
const REALISTIC = {
  searched_hit: { kb_state: "searched_hit", kb_hits: [HIT_A, HIT_B] },
  searched_miss: { kb_state: "searched_miss", kb_hits: [] },
  search_error: {
    kb_state: "search_error",
    kb_hits: [],
    kb_failed_collections: [7, 9],
  },
  partial_error: {
    kb_state: "partial_error",
    kb_hits: [HIT_A, HIT_B],
    kb_failed_collections: [9],
  },
};

const EVERY_SEARCHED_STATE = Object.keys(REALISTIC);

/** CSP 命中時同時填的 drawer 契約(同函式 :688-696)。 */
function citationsFor(hits) {
  return hits.map((h, i) => ({
    id: `kb:${h.collection_id}:${h.document_id}:${i + 1}`,
    title: h.filename,
    score: h.score,
    snippet: h.content.slice(0, 200),
  }));
}

function assistantMsg(fragment, overrides = {}) {
  return {
    id: 42,
    role: "assistant",
    text: "以下是回答。",
    streaming: false,
    siblingIndex: 0,
    siblingCount: 1,
    siblingIds: [42],
    citations: citationsFor(fragment?.kb_hits || []),
    ...kbMetaFields(fragment),
    ...overrides,
  };
}

function renderBubble(msg) {
  return render(
    <MessageBubble msg={msg} agents={[]} conversationId={1} />,
  );
}

/** 畫面上所有 kb 記號(不管哪一個狀態)。 */
function kbMarkers(root = document.body) {
  return [...root.querySelectorAll('[data-testid^="kb-state-"]')];
}

// ---------------------------------------------------------------------------
// 元件層:徽章本身說了什麼
// ---------------------------------------------------------------------------

describe("五狀態徽章 — 渲染", () => {
  afterEach(cleanup);

  it("四個「查過了」的狀態各自畫出自己的徽章,而且互相分得出來", () => {
    const seen = new Set();
    for (const state of EVERY_SEARCHED_STATE) {
      const { container } = renderBubble(assistantMsg(REALISTIC[state]));
      const badge = container.querySelector(`[data-testid="kb-state-${state}"]`);
      expect(badge, `${state} 應該畫出自己的徽章`).toBeTruthy();
      // 同一輪裡不得同時出現第二個狀態的記號(反推狀態會這樣壞)。
      expect(kbMarkers(container).map((n) => n.dataset.testid)).toEqual([
        `kb-state-${state}`,
      ]);
      seen.add(badge.textContent.trim());
      cleanup();
    }
    // 四句話彼此不同——四個狀態共用同一句話等於只有一個狀態。
    expect(seen.size).toBe(EVERY_SEARCHED_STATE.length);
  });

  it("not_searched 與欄位缺席都不畫任何 kb 記號(這是決定,不是壞掉)", () => {
    // 兩種來源在畫面上必須完全一樣安靜:
    //   * not_searched —— 全院一個庫都沒標記,聊天內容不該因為這個功能改變
    //   * 欄位缺席 —— 這個功能上線之前存下來的舊訊息
    for (const fragment of [{ kb_state: "not_searched", kb_hits: [] }, undefined]) {
      const { container } = renderBubble(assistantMsg(fragment));
      expect(kbMarkers(container)).toHaveLength(0);
      // 而且答案本身照常在(安靜 ≠ 把整則訊息吃掉)。
      expect(container.textContent).toContain("以下是回答。");
      cleanup();
    }
  });

  it("認不得的狀態字串也不畫(未來多一個狀態時寧可安靜,不要亂講)", () => {
    const { container } = renderBubble(
      assistantMsg({ kb_state: "searched_probably", kb_hits: [HIT_A] }),
    );
    expect(kbMarkers(container)).toHaveLength(0);
  });

  it("「沒命中」與「查不了」是兩句不同的話", () => {
    const miss = renderBubble(assistantMsg(REALISTIC.searched_miss));
    const missText = miss.container.querySelector(
      '[data-testid="kb-state-searched_miss"]',
    ).textContent;
    cleanup();
    const err = renderBubble(assistantMsg(REALISTIC.search_error));
    const errText = err.container.querySelector(
      '[data-testid="kb-state-search_error"]',
    ).textContent;

    // 沒命中:查成功了,只是院規裡沒有;答案是模型的一般知識。
    expect(missText).toContain("沒找到相關條文");
    expect(missText).toContain("一般知識");
    // 查不了:根本沒查成,不知道院規裡有沒有。
    expect(errText).toContain("失敗");
    // 最要命的那一種說謊:把「查不了」講成「查過了、沒有」。
    expect(errText).not.toContain("沒找到相關條文");
    expect(missText).not.toContain("失敗");
    expect(missText).not.toBe(errText);
  });

  it("partial_error 讓使用者看得出「這不是全部的依據」", () => {
    const { container } = renderBubble(assistantMsg(REALISTIC.partial_error));
    const badge = container.querySelector('[data-testid="kb-state-partial_error"]');
    // 失敗庫的數量要說出來,而且要明說依據不完整。
    expect(badge.textContent).toContain("1");
    expect(badge.textContent).toMatch(/不是全部|不完整/);
    // 但查到的那兩份還是要看得見——「不完整」不等於「什麼都不給」。
    expect(badge.textContent).toContain("人事管理規則.pdf");
    expect(badge.textContent).toContain("差勤作業要點.docx");
  });

  it("查得乾淨的命中不得無中生有地說依據不完整", () => {
    const { container } = renderBubble(assistantMsg(REALISTIC.searched_hit));
    const badge = container.querySelector('[data-testid="kb-state-searched_hit"]');
    expect(badge.textContent).not.toMatch(/不是全部|不完整|失敗/);
  });
});

describe("命中徽章 — 文件名、原文與分數", () => {
  afterEach(cleanup);

  it("徽章列出每一筆命中的文件名", () => {
    const { container } = renderBubble(assistantMsg(REALISTIC.searched_hit));
    const chips = [...container.querySelectorAll('[data-testid^="kb-source-"]')];
    expect(chips).toHaveLength(2);
    // 顯示的是**文件名**,不是 document_id / collection_id。
    expect(chips.map((c) => c.textContent)).toEqual([
      expect.stringContaining("人事管理規則.pdf"),
      expect.stringContaining("差勤作業要點.docx"),
    ]);
    expect(container.textContent).not.toContain("21");
    expect(container.textContent).not.toContain("34");
  });

  it("hover 泡泡帶原文與信心分數", () => {
    const { container } = renderBubble(assistantMsg(REALISTIC.searched_hit));
    const chips = [...container.querySelectorAll('[data-testid^="kb-source-"]')];
    const first = chips[0].getAttribute("title");
    // 原文(完整,不是截斷過的 snippet)
    expect(first).toContain("第三條 差勤一律採線上簽核，紙本不再受理。");
    // 信心分數,沿用抽屜的百分比寫法(trust.jsx:102)
    expect(first).toContain("91%");
    // 第二筆是自己的原文與分數,不是第一筆的複本。
    expect(chips[1].getAttribute("title")).toContain("第五條 出差應於事前完成申請程序。");
    expect(chips[1].getAttribute("title")).toContain("78%");
  });

  it("引用不顯示頁碼(擁有者裁決:規章的定位點是條號)", () => {
    // 上游哪天多送一個 page 欄位,也不准漏到畫面上——已匯入文件的頁碼是
    // 已知會錯的值,顯示它比不顯示更糟。
    const { container } = renderBubble(
      assistantMsg({
        kb_state: "searched_hit",
        kb_hits: [{ ...HIT_A, page: 12 }],
      }),
    );
    const badge = container.querySelector('[data-testid="kb-state-searched_hit"]');
    expect(badge.textContent).not.toMatch(/頁|p\.\s*\d/);
    const chip = container.querySelector('[data-testid^="kb-source-"]');
    expect(chip.getAttribute("title")).not.toMatch(/頁|p\.\s*\d/);
  });

  it("命中時既有的來源管線照常運作(不新建渲染機制)", () => {
    const fragment = REALISTIC.searched_hit;
    const { container } = renderBubble(
      assistantMsg(fragment, { text: "依規定[1],並參照[2]。" }),
    );
    // renderTextWithCitations 的 [N] 行內標記
    expect(container.textContent).toContain("[1]");
    expect(container.textContent).toContain("[2]");
    // 既有的「查看 N 筆來源」footer
    expect(container.textContent).toContain("查看 2 筆來源");
  });
});

// ---------------------------------------------------------------------------
// 兩個映射縫 —— 各自獨立
// ---------------------------------------------------------------------------

describe("映射縫", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("applyMeta(SSE 現場)這一縫:串完的當下徽章就在", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("依規定辦理如附。", {
      meta: {
        ...REALISTIC.searched_hit,
        citations: citationsFor(REALISTIC.searched_hit.kb_hits),
      },
    });
    await mountOrchestrator({ backend });

    await sendText("差勤要怎麼簽?");
    await waitForAnswer("依規定辦理如附。");
    await waitForIdle();

    // ⚠ 這一條**沒有**重新載入。它單獨釘住 applyMeta;把 mapServerMessage
    // 那一縫拿掉,這條照樣綠。
    const badge = await screen.findByTestId("kb-state-searched_hit");
    expect(badge.textContent).toContain("人事管理規則.pdf");
  });

  it("mapServerMessage(重新載入)這一縫:從伺服器讀回來的舊訊息徽章也在", async () => {
    const backend = createFakeBackend({
      conversations: [
        {
          id: 500,
          title: "規章問答",
          agent_id: null,
          classified: false,
          tags: [],
          starred: false,
          folder: "all",
        },
      ],
    });
    backend.disableTitleGeneration();
    // 伺服器上已經有的一輪(這一輪不經過 SSE,所以完全不碰 applyMeta)。
    backend.route("GET", /^\/api\/conversations\/500$/, (req, { jsonResponse }) =>
      jsonResponse({
        id: 500,
        title: "規章問答",
        agent_id: null,
        classified: false,
        tags: [],
        starred: false,
        folder: "all",
        active_leaf_message_id: 9002,
        messages: [
          {
            id: 9001,
            role: "user",
            content: "差勤要怎麼簽?",
            parent_id: null,
            metadata: {},
          },
          {
            id: 9002,
            role: "assistant",
            content: "依規定辦理如附。",
            parent_id: 9001,
            metadata: {
              ...REALISTIC.searched_hit,
              citations: citationsFor(REALISTIC.searched_hit.kb_hits),
            },
          },
        ],
      }),
    );
    await mountOrchestrator({ backend });
    await selectConversation("規章問答");

    await waitForAnswer("依規定辦理如附。");
    // ⚠ 這一條**沒有送過任何一輪**。它單獨釘住 mapServerMessage;把 applyMeta
    // 那一縫拿掉,這條照樣綠。
    const badge = await screen.findByTestId("kb-state-searched_hit");
    expect(badge.textContent).toContain("人事管理規則.pdf");
  });

  it("mapServerMessage 這一縫:沒有 kb 欄位的舊訊息載回來,畫面上零 kb 記號", async () => {
    const backend = createFakeBackend({
      conversations: [
        {
          id: 501,
          title: "上線前的舊對話",
          agent_id: null,
          classified: false,
          tags: [],
          starred: false,
          folder: "all",
        },
      ],
    });
    backend.disableTitleGeneration();
    backend.route("GET", /^\/api\/conversations\/501$/, (req, { jsonResponse }) =>
      jsonResponse({
        id: 501,
        title: "上線前的舊對話",
        agent_id: null,
        classified: false,
        tags: [],
        starred: false,
        folder: "all",
        active_leaf_message_id: 8002,
        messages: [
          { id: 8001, role: "user", content: "隨便問問", parent_id: null, metadata: {} },
          {
            id: 8002,
            role: "assistant",
            content: "這是上線前存下來的答案。",
            parent_id: 8001,
            // 這個功能存在之前存的訊息:metadata 裡一個 kb_ 欄位都沒有。
            metadata: { trace: [], citations: [] },
          },
        ],
      }),
    );
    await mountOrchestrator({ backend });
    await selectConversation("上線前的舊對話");

    await waitForAnswer("這是上線前存下來的答案。");
    expect(kbMarkers()).toHaveLength(0);
  });

  it("kb_* 真的存得進伺服器(重新整理之後才有東西可讀)", async () => {
    // 重新載入那一縫再怎麼接好,存的時候掉了就沒有意義。
    // `runtime/messageMeta.js` 的 `buildPersistMeta` 是 `{...finalMeta}` 起手,
    // kb_* 是整包抵達的欄位(不像 trace 要跨 frame 累積),所以照理會原樣穿過
    // ——「照理」不算證據,這一條去看真的送到後端的那個 body。
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("依規定辦理如附。", {
      meta: {
        ...REALISTIC.partial_error,
        citations: citationsFor(REALISTIC.partial_error.kb_hits),
      },
    });
    await mountOrchestrator({ backend });

    await sendText("差勤要怎麼簽?");
    await waitForAnswer("依規定辦理如附。");
    await waitForIdle();

    const convId = backend.conversationIds().at(-1);
    const assistant = backend
      .storedMessages(convId)
      .find((m) => m.role === "assistant");
    expect(assistant, "助理訊息應該已經寫回後端").toBeTruthy();
    expect(assistant.metadata.kb_state).toBe("partial_error");
    expect(assistant.metadata.kb_failed_collections).toEqual([9]);
    expect(assistant.metadata.kb_hits.map((h) => h.filename)).toEqual([
      "人事管理規則.pdf",
      "差勤作業要點.docx",
    ]);
    // 原文要跟著存,否則重新整理之後 hover 泡泡就是空的。
    expect(assistant.metadata.kb_hits[0].content).toContain("第三條");
  });

  it("送出一輪之後切走再切回來,現場那則的狀態不會在切換中掉", async () => {
    // ⚠ 這一條**不是**重新載入。app.jsx:1062 的
    // `if (messagesByConv[selectedConvId]?.length) return;` 讓已經在記憶體裡
    // 的對話**不重新抓**,所以切走再切回來走的是同一批 applyMeta 蓋出來的列。
    // 真正的重新載入(整頁重開、或這個分頁沒開過的對話)由上面那條
    // mapServerMessage 的測試守 —— 兩件事分開釘,才知道壞的是哪一件。
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend
      .enqueueAnswer("依規定辦理如附。", {
        meta: {
          ...REALISTIC.partial_error,
          citations: citationsFor(REALISTIC.partial_error.kb_hits),
        },
      })
      .enqueueAnswer("另一個對話的回答");
    await mountOrchestrator({ backend });

    await sendText("差勤要怎麼簽?");
    await waitForAnswer("依規定辦理如附。");
    await waitForIdle();

    await clickNewChat();
    await sendText("不相干的問題");
    await waitForAnswer("另一個對話的回答");
    await waitForIdle();

    await selectConversation("差勤要怎麼簽?");
    await waitFor(() => {
      expect(screen.getByText("依規定辦理如附。")).toBeInTheDocument();
    });

    const badge = await screen.findByTestId("kb-state-partial_error");
    expect(badge.textContent).toMatch(/不是全部|不完整/);
    expect(badge.textContent).toContain("人事管理規則.pdf");
  });
});
