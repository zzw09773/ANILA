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

/** CSP 命中時同時填的 drawer 契約(同函式 :688-696)。 */
function citationsFor(hits) {
  return hits.map((h, i) => ({
    id: `kb:${h.collection_id}:${h.document_id}:${i + 1}`,
    title: h.filename,
    score: h.score,
    snippet: h.content.slice(0, 200),
  }));
}

/**
 * 上線前的舊訊息:**帶著下游來源、但一個 kb_ 欄位都沒有**。
 *
 * 這個 fixture 存在的理由(驗收 I1):原本所有「欄位缺席」的 fixture 都**沒有
 * citations**,於是「kb_state 缺席時就從 citations 反推 searched_hit」這種壞法
 * 整套 636 條一條都攔不住。而真實世界的舊訊息**是會帶 citations 的**——下游
 * agent／RAG 自己給的來源(後端所有 citation 生產者的預設是空陣列,但只是預設)。
 * 那樣的話每一則有引用的舊答案都會宣稱「依據院內規章」,**正是本包要消滅的那句謊**。
 */
const DOWNSTREAM_ONLY_META = {
  citations: [
    { id: "ds-1", title: "下游 agent 給的來源", snippet: "與院規無關的一段話。" },
    { id: "ds-2", title: "另一份下游來源", snippet: "也與院規無關。" },
  ],
};

function assistantMsg(fragment, overrides = {}) {
  return {
    id: 42,
    role: "assistant",
    text: "以下是回答。",
    streaming: false,
    siblingIndex: 0,
    siblingCount: 1,
    siblingIds: [42],
    // meta 自己帶 citations 就用它(下游來源的情況);否則照命中推出來。
    citations: fragment?.citations || citationsFor(fragment?.kb_hits || []),
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

  it("來源抽屜在時只移除重複的命中徽章,無來源的命中仍明示狀態", () => {
    const withSources = renderBubble(assistantMsg(REALISTIC.searched_hit));
    expect(kbMarkers(withSources.container)).toHaveLength(0);
    expect(withSources.container.textContent).toContain("查看 2 筆來源");
    cleanup();

    const withoutSources = renderBubble(
      assistantMsg({ ...REALISTIC.searched_hit, citations: [] }),
    );
    const badge = withoutSources.container.querySelector(
      '[data-testid="kb-state-searched_hit"]',
    );
    expect(badge).toBeTruthy();
    expect(badge.textContent).toContain("依據院內規章");
    expect(withoutSources.container.textContent).not.toContain("查看 ");
  });

  it("枚舉五態與欄位缺席,不讓薄化製造新的靜默", () => {
    const cases = [
      ["searched_hit 有來源", REALISTIC.searched_hit, false, true],
      [
        "searched_hit 無來源",
        { ...REALISTIC.searched_hit, citations: [] },
        true,
        false,
      ],
      ["searched_miss", REALISTIC.searched_miss, true, false],
      ["search_error", REALISTIC.search_error, true, false],
      ["partial_error", REALISTIC.partial_error, true, true],
      ["not_searched", { kb_state: "not_searched", kb_hits: [] }, false, false],
      ["欄位缺席", undefined, false, false],
    ];

    for (const [name, fragment, wantsBadge, wantsDrawer] of cases) {
      const { container } = renderBubble(assistantMsg(fragment));
      expect(
        kbMarkers(container).length > 0,
        `${name} 的 badge 表面不符合預期`,
      ).toBe(wantsBadge);
      expect(
        container.textContent.includes("查看 2 筆來源"),
        `${name} 的來源抽屜 affordance 不符合預期`,
      ).toBe(wantsDrawer);
      cleanup();
    }
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

  it("舊訊息帶著下游來源、但沒有 kb_state:一樣零個 kb 記號", () => {
    // 「安靜」必須是從 **kb_state 缺席** 推出來的,不是從「反正也沒有來源」
    // 推出來的。有來源、但那些來源不是院規 —— 這正是最容易被反推誤判的一格。
    const { container } = renderBubble(
      assistantMsg(DOWNSTREAM_ONLY_META, { text: "根據既有資料[1]與[2]。" }),
    );
    expect(kbMarkers(container)).toHaveLength(0);
    // ⚠ 而且安靜**不是**靠把 citations 弄壞換來的:既有來源管線照常運作,
    // 否則這條測試就會用「把來源整個弄不見」的方式假裝通過。
    expect(container.textContent).toContain("查看 2 筆來源");
    expect(container.textContent).toContain("[1]");
    expect(container.textContent).toContain("[2]");
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
    // 沒有 citations 時 badge 不能因為薄化規則消失,才能檢查它的原文案。
    const { container } = renderBubble(
      assistantMsg({ ...REALISTIC.searched_hit, citations: [] }),
    );
    const badge = container.querySelector('[data-testid="kb-state-searched_hit"]');
    expect(badge.textContent).not.toMatch(/不是全部|不完整|失敗/);
  });
});

describe("命中徽章 — 文件名、原文與分數", () => {
  afterEach(cleanup);

  it("徽章列出每一筆命中的文件名", () => {
    const { container } = renderBubble(
      assistantMsg({ ...REALISTIC.searched_hit, citations: [] }),
    );
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
    const { container } = renderBubble(
      assistantMsg({ ...REALISTIC.searched_hit, citations: [] }),
    );
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
        citations: [],
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
// 行內引用的 hover(trust.jsx:12)
//
// 規章引用**永遠沒有 `section`**(`proxy.py:688-696` 只給 id/title/score/snippet,
// 而全樹沒有任何 citation 生產者填過 section)。在 Task 6 之前 `[N]` 幾乎不會出現,
// 所以沒人看得到;**這個功能就是讓它現形的那一包**——而它現形的地方是一個
// 信任表面:使用者正把游標停在那裡查證這句話的依據是什麼。
// 抽屜端本來就有守衛(`trust.jsx:107` 的 `c.section &&`),只有行內 hover 沒有,
// 於是同一份資料的兩個表面講了不一樣的話——與本功能的誠實不變式同型。
// ---------------------------------------------------------------------------

describe("行內引用的 hover", () => {
  afterEach(cleanup);

  /** 內文裡的 `[N]` 行內按鈕(CitationInline 渲染成 `[n]`)。 */
  function inlineCitation(container, n) {
    return [...container.querySelectorAll("button")].find(
      (b) => b.textContent === `[${n}]`,
    );
  }

  it("沒有 section 的引用,hover 不會冒出 undefined", () => {
    const { container } = renderBubble(
      assistantMsg(REALISTIC.searched_hit, { text: "依規定辦理[1]。" }),
    );
    const inline = inlineCitation(container, 1);
    expect(inline, "行內 [1] 按鈕應該有渲染").toBeTruthy();
    const title = inline.getAttribute("title");
    expect(title).not.toContain("undefined");
    // 也不要留下一個沒有右半邊的分隔號(「檔名 · 」)。
    expect(title.trim()).toBe("人事管理規則.pdf");
  });

  it("有 section 的引用維持既有的「標題 · 章節」形式(不回歸下游 RAG 的情況)", () => {
    const { container } = renderBubble(
      assistantMsg(
        {
          citations: [
            { id: "rag-1", title: "作業手冊.pdf", section: "第 3 章 權責", snippet: "…" },
          ],
        },
        { text: "見手冊[1]。" },
      ),
    );
    expect(inlineCitation(container, 1).getAttribute("title")).toBe(
      "作業手冊.pdf · 第 3 章 權責",
    );
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
        // 命中但沒有 citations 時,兩個映射縫都仍要把狀態顯示出來。
        citations: [],
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
              // 同上:這一輪沒有來源抽屜,不能因為隱藏重複 badge 而靜默。
              citations: [],
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
            // 這個功能存在之前存的訊息:metadata 裡一個 kb_ 欄位都沒有,
            // 但**帶著下游 agent 自己給的來源**——空的 citations 會讓
            // 「從 citations 反推狀態」這條路在這一縫上也沒有釘子(驗收 I1)。
            metadata: { trace: [], ...DOWNSTREAM_ONLY_META },
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
