import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render } from "@testing-library/react";
import React from "react";

import { MessageBubble } from "../chat.jsx";
import { kbMetaFields } from "../app.jsx";
import { coverageFromMessage } from "../runtime/coverageFooter.js";

const HIT_A = {
  collection_id: 7,
  document_id: 21,
  filename: "人事管理規則.pdf",
  content: "第三條 差勤一律採線上簽核。",
  score: 0.91,
};
const HIT_B = {
  collection_id: 7,
  document_id: 34,
  filename: "差勤作業要點.docx",
  content: "第五條 出差應於事前完成申請。",
  score: 0.78,
};

function citationsFor(hits) {
  return hits.map((hit, index) => ({
    id: `kb:${hit.collection_id}:${hit.document_id}:${index + 1}`,
    title: hit.filename,
    score: hit.score,
    snippet: hit.content.slice(0, 200),
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
    citations: fragment?.citations || citationsFor(fragment?.kb_hits || []),
    ...kbMetaFields(fragment),
    ...overrides,
  };
}

afterEach(cleanup);

describe("coverageFromMessage", () => {
  it("查了的是文件數，引用只算對得上的 [N]", () => {
    const coverage = coverageFromMessage(assistantMsg(
      { kb_state: "searched_hit", kb_hits: [HIT_A, HIT_B] },
      { text: "模型自己說查了 9 份、引用 8 份。實際只用到差勤要點[2]。" },
    ));
    expect(coverage.searched).toBe(2);
    expect(coverage.sent).toBe(2);
    expect(coverage.cited).toBe(1);
    expect(coverage.line).toContain("查了 2 份、引用 1 份");
    expect(coverage.line).toContain("送進模型 2 份");
    expect(coverage.line).toContain("未找到不等於不存在");
    expect(coverage.line).not.toContain("查了 9 份");
    expect(coverage.line).not.toContain("引用 8 份");
  });

  it("同一份文件的多段只算一份", () => {
    const second = { ...HIT_A, content: "第四條 補休依當月結算。" };
    const coverage = coverageFromMessage(assistantMsg(
      { kb_state: "searched_hit", kb_hits: [HIT_A, second] },
      { text: "見[1]與[2]。" },
    ));
    expect(coverage.searched).toBe(1);
    expect(coverage.sent).toBe(1);
    expect(coverage.cited).toBe(1);
  });

  it("對不上來源的編號不算引用", () => {
    const coverage = coverageFromMessage(assistantMsg(
      { kb_state: "searched_hit", kb_hits: [HIT_A, HIT_B] },
      { text: "見[9]。" },
    ));
    expect(coverage.cited).toBe(0);
    expect(coverage.line).toContain("查了 2 份、引用 0 份");
  });

  it("知識庫沒命中也沒引用時，不暗示沒有來源（助手可能用自己的資料庫）", () => {
    const coverage = coverageFromMessage(assistantMsg({
      kb_state: "searched_miss",
      kb_hits: [],
    }));
    expect(coverage.line).toBe(
      "本次未列出可追溯的文件引用（助手可能使用自己的資料庫或工具）。",
    );
    expect(coverage.line).not.toContain("查了 0 份");
  });

  it("有工具呼叫紀錄時列出工具名稱", () => {
    const coverage = coverageFromMessage(assistantMsg({
      kb_state: "not_searched",
      kb_hits: [],
    }, {
      tool_calls: [{ name: "社團名冊查詢" }, { name: "社團名冊查詢" }, { function: { name: "場地查詢" } }],
    }));
    expect(coverage.line).toBe(
      "本次未列出可追溯的文件引用（助手可能使用自己的資料庫或工具）。這次用了工具：社團名冊查詢、場地查詢。",
    );
  });

  it("沒有 kb_state 時不從下游來源反推查過", () => {
    expect(coverageFromMessage({
      role: "assistant",
      streaming: false,
      text: "根據既有資料[1]。",
      citations: [{ id: "ds-1", title: "下游來源" }],
    })).toBeNull();
  });

  it("串流中先不顯示，避免用半截正文計數", () => {
    expect(coverageFromMessage(assistantMsg(
      { kb_state: "searched_hit", kb_hits: [HIT_A] },
      { streaming: true, text: "見[1]" },
    ))).toBeNull();
  });

  it("認不得的狀態不畫", () => {
    expect(coverageFromMessage(assistantMsg({
      kb_state: "searched_probably",
      kb_hits: [HIT_A],
    }))).toBeNull();
  });

  it("思考標籤裡的編號不算進使用者看得到的引用", () => {
    const coverage = coverageFromMessage(assistantMsg(
      { kb_state: "searched_hit", kb_hits: [HIT_A, HIT_B] },
      { text: "<think>先看[1]</think>正文沒有引用。" },
    ));
    expect(coverage.cited).toBe(0);
  });

  it("內文已標明是摘錄時，列出檔名", () => {
    const coverage = coverageFromMessage(assistantMsg({
      kb_state: "searched_hit",
      kb_hits: [{
        ...HIT_A,
        content: "以下是文件摘錄，不是全文。\n\n第三條",
      }, HIT_B],
    }, { text: "見[1]。" }));
    expect(coverage.excerpted).toEqual(["人事管理規則.pdf"]);
    expect(coverage.line).toContain("摘錄、不是全文：人事管理規則.pdf。");
    expect(coverage.line).not.toContain("21");
  });

  it("附件整份沒進來時補上一句，不捏造檔名", () => {
    const coverage = coverageFromMessage(
      assistantMsg({ kb_state: "not_searched", kb_hits: [] }),
      { attachmentsOmitted: true },
    );
    expect(coverage.line).toContain("附件未納入這次回答。");
    expect(coverage.line).not.toMatch(/\.pdf|\.docx/);
  });
});

describe("CoverageFooter on the answer", () => {
  it("畫在回答上，而且不採用模型自己報的份數", () => {
    const { container } = render(
      <MessageBubble
        msg={assistantMsg(
          { kb_state: "searched_hit", kb_hits: [HIT_A, HIT_B] },
          { text: "查了 9 份、引用 8 份。依要點[2]辦理。" },
        )}
        agents={[]}
        conversationId={1}
      />,
    );
    const footer = container.querySelector('[data-testid="coverage-footer"]');
    expect(footer).toBeTruthy();
    expect(footer.textContent).toContain("查了 2 份、引用 1 份");
    expect(footer.textContent).toContain("送進模型 2 份");
    expect(footer.textContent).toContain("未找到不等於不存在");
    expect(footer.textContent).not.toContain("查了 9 份");
  });

  it("舊訊息沒有檢索狀態時不出現這一行", () => {
    const { container } = render(
      <MessageBubble
        msg={{
          id: 7,
          role: "assistant",
          text: "根據既有資料[1]。",
          streaming: false,
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [7],
          citations: [{ id: "ds-1", title: "下游來源", snippet: "一段話" }],
        }}
        agents={[]}
        conversationId={1}
      />,
    );
    expect(container.querySelector('[data-testid="coverage-footer"]')).toBeNull();
    expect(container.textContent).toContain("查看 1 筆來源");
  });
});
