// UX-IDEAS ①②:畫面上不能出現實作詞;列管對話被擋時要說得出「哪一級、擋掉
// 什麼、能不能自己解開」。
//
// 為什麼是這個形狀的測試
// ----------------------
// 1. **行為層**:文案是純函式(uxCopy.js),直接斷言使用者真的會讀到的字串;
//    列管拒絕另外真的把 <MessageBubble> 渲染出來,量的是 title 屬性本身,
//    不是「我們有呼叫某個函式」。
// 2. **原始碼層護欄**:真正會壞的是**呼叫端**。uxCopy 全綠而 app.jsx 的
//    handoffToAgent 還在寫 `[Router] …交接給…`,行為測試一個都不會紅——
//    而那一行會被存進逐字稿、被匯出、被再讀一次。所以直接讀原始碼把它釘住。

// @source-text-guard — 本檔含「讀原始碼 + toContain」的字串比對測試。
// 這類斷言只證明某段文字還在檔案裡,**不證明它在執行時會發生**:
// 呼叫端整個被繞過、狀態沒接上、時序錯了,它照樣綠。
// 它們擋的是「整段被刪掉」,不能當成行為覆蓋率。
// 對應的行為測試在 src/__tests__/orchestrator*.test.jsx。

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import React from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  classifiedCopyDenial,
  classifiedShareDenial,
  handoffNotice,
} from "../uxCopy.js";
import { MessageBubble } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

afterEach(cleanup);

// vitest 的 cwd 是 apps/anila-shell(package root)。
const src = (rel) => readFileSync(resolve(process.cwd(), "src", rel), "utf8");

// 使用者永遠不該在畫面上讀到的詞。中文的「交接」也在內:它是我們的說法,
// 已定案的使用者說法是「交給其他助手」/「改由…接手」。
const IMPLEMENTATION_WORDS = [
  "Router",
  "router",
  "handoff",
  "HANDOFF",
  "Handoff",
  "bypass",
  "dispatch",
  "DISPATCH",
  "Auto route",
  "Direct target",
  "交接",
  "分派",
];

function assertNoImplementationWords(text) {
  for (const word of IMPLEMENTATION_WORDS) {
    expect(text).not.toContain(word);
  }
}

// ---- ①  助手轉手,寫進逐字稿的那一行 --------------------------------------
describe("handoffNotice — 寫進對話逐字稿的轉手訊息", () => {
  it("一個實作詞都不留", () => {
    assertNoImplementationWords(handoffNotice("法規助手", "影像助手"));
    assertNoImplementationWords(handoffNotice(null, "影像助手"));
  });

  it("講得出是誰接手,以及上下文有沒有跟著走", () => {
    const line = handoffNotice("法規助手", "影像助手");
    expect(line).toContain("法規助手");
    expect(line).toContain("影像助手");
    expect(line).toContain("接手");
    expect(line).toContain("先前的對話內容會一併帶過去");
  });

  it("不知道原本是誰時仍然是完整的一句話", () => {
    expect(handoffNotice(undefined, "影像助手")).toBe(
      "已改由「影像助手」接手，先前的對話內容會一併帶過去。",
    );
  });
});

describe("原始碼護欄:轉手訊息真的由 handoffNotice 產生", () => {
  const appSrc = src("app.jsx");

  it("app.jsx 的 handoffToAgent 用 handoffNotice 當訊息內容", () => {
    expect(appSrc).toContain("text: handoffNotice(fromLabel, label),");
  });

  it("app.jsx 不再有任何 [Router] 前綴的使用者字串", () => {
    expect(appSrc).not.toContain("[Router]");
  });

  it("app.jsx 的分享鈕提示帶著等級,不再是只讀 boolean 的一句話", () => {
    expect(appSrc).toContain(
      "classifiedShareDenial(selectedConv.classificationLevel)",
    );
    expect(appSrc).not.toContain('"列管對話不可分享"');
  });

  it("markdown.jsx / multiagent.jsx 的使用者字串不含實作詞", () => {
    expect(src("markdown.jsx")).not.toContain("（Router");
    expect(src("multiagent.jsx")).not.toContain("agent handoff");
  });
});

// ---- ②  列管對話被擋時,說得出是哪一級 ------------------------------------
describe("classifiedCopyDenial — 拒絕複製時說得出等級", () => {
  it("密與機密各自報出自己的等級,並說明不可分享、無法自行解除", () => {
    for (const level of ["密", "機密"]) {
      const text = classifiedCopyDenial(level);
      expect(text).toContain(level);
      expect(text).toContain("不可複製");
      expect(text).toContain("不可分享");
      expect(text).toContain("此狀態無法由使用者自行解除");
    }
  });

  it("密與機密不會互相冒名", () => {
    expect(classifiedCopyDenial("密")).not.toBe(classifiedCopyDenial("機密"));
    expect(classifiedCopyDenial("密")).toContain("「密」");
    expect(classifiedCopyDenial("機密")).toContain("「機密」");
  });

  it("營業秘密講的是稽核,不是「不可分享」", () => {
    const text = classifiedCopyDenial("營業秘密");
    expect(text).toContain("營業秘密");
    expect(text).toContain("稽核");
    expect(text).not.toContain("不可分享");
  });

  it("缺欄位的舊酬載退回只講「列管」的說法,不亂猜等級", () => {
    const text = classifiedCopyDenial(undefined);
    expect(text).toContain("列管對話不可複製");
    for (const level of ["營業秘密", "機密"]) {
      expect(text).not.toContain(level);
    }
  });
});

describe("classifiedShareDenial — 拒絕分享時說得出等級", () => {
  it("密與機密各自報出自己的等級", () => {
    expect(classifiedShareDenial("密")).toContain("「密」");
    expect(classifiedShareDenial("機密")).toContain("「機密」");
    expect(classifiedShareDenial("機密")).toContain("此狀態無法由使用者自行解除");
  });
});

// 真的把泡泡渲染出來,量 title 屬性——「有呼叫函式」不算證明。
function renderBubble(classificationLevel) {
  return render(
    <ConfirmProvider>
      <MessageBubble
        msg={{
          id: "m1",
          role: "assistant",
          text: "測試內容",
          streaming: false,
        }}
        agents={[]}
        conversationId={1}
        classified={true}
        classificationLevel={classificationLevel}
      />
    </ConfirmProvider>,
  );
}

describe("MessageBubble:列管對話的複製鈕真的掛上帶等級的說明", () => {
  it("機密對話", () => {
    renderBubble("機密");
    expect(screen.getByTitle(classifiedCopyDenial("機密"))).toBeTruthy();
  });

  it("營業秘密對話拿到的是不一樣的說明", () => {
    renderBubble("營業秘密");
    expect(screen.getByTitle(classifiedCopyDenial("營業秘密"))).toBeTruthy();
    expect(screen.queryByTitle(classifiedCopyDenial("機密"))).toBeNull();
  });
});
