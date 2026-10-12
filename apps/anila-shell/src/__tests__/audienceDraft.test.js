import { beforeEach, describe, expect, it } from "vitest";

import {
  AUDIENCE_OPTIONS,
  applyAudienceChoice,
  draftWordingDependsOnAudience,
  prepareDraftSend,
  recallDraftAudience,
  rememberDraftAudience,
  resetDraftAudienceMemory,
} from "../runtime/audienceDraft.js";

const LABELS = ["給客戶", "給同團隊", "給主管", "給承辦"];

const NEEDS_AUDIENCE = [
  "幫我寫一封延期通知，說明週五的會議改到下週一",
  "起草一則公告，告訴大家大廳整修",
  "請把下面這段改寫成正式公文",
  "擬一封道歉信，說明樣品晚到",
  "幫我潤稿這封郵件，語氣再穩一點",
  "寫一封信跟對方說報價可以延長三天",
  "草擬一份簽呈，請同意採購備品",
  "幫我寫一則回覆，說明資料下週才能補齊",
  "請撰寫一則簡訊，通知參觀時間改期",
  "這段寫給客戶看的信幫我改寫得更短",
  "幫我寫一段對外的進度說明",
  "幫我寫信跟對方說改期",
  "擬一份週報，整理這週完成的三件事",
];

const DOES_NOT_NEED_AUDIENCE = [
  "這份規章第 3 條在說什麼",
  "幫我寫一段 Python，把兩欄數字加總",
  "翻譯這段成英文：會議改到下週一",
  "今天的會議幾點開始",
  "幫我查承辦窗口在哪一層",
  "信裡的金額是多少",
  "寫一個 SQL 把通知表算出來",
  "什麼是公文格式",
  "你好",
  "幫我總結這份附件",
  "",
  "給客戶",
  "幫我寫一份會議紀錄，把決議列出來",
  "公文要怎麼寫",
  "翻譯這封信",
];

beforeEach(() => {
  sessionStorage.clear();
  resetDraftAudienceMemory();
});

describe("起草是否要先問對象", () => {
  it("換對象就會寫成另一份的起草要問", () => {
    for (const text of NEEDS_AUDIENCE) {
      expect(draftWordingDependsOnAudience(text), text).toBe(true);
    }
  });

  it("查詢、程式、翻譯、會議紀錄不問", () => {
    for (const text of DOES_NOT_NEED_AUDIENCE) {
      expect(draftWordingDependsOnAudience(text), text).toBe(false);
    }
  });

  it("四個選項固定，偵測本身不選", () => {
    expect(AUDIENCE_OPTIONS.map((option) => option.label)).toEqual(LABELS);
    expect(new Set(AUDIENCE_OPTIONS.map((option) => option.id)).size).toBe(4);
    const letter = "幫我寫一封信給客戶，也給主管看";
    expect(prepareDraftSend(letter)).toEqual({ send: false, text: letter });
    expect(recallDraftAudience(letter)).toBeNull();
  });
});

describe("記住這份草稿的選擇", () => {
  const letter = "幫我寫一封延期通知，說明週五的會議改到下週一";

  it("沒點過就沒有答案，文句裡的對象也不算", () => {
    expect(recallDraftAudience(letter)).toBeNull();
    expect(recallDraftAudience("給客戶、給同團隊、給主管、給承辦，幫我寫一封信")).toBeNull();
    expect(rememberDraftAudience(letter, "給客戶")).toBe(false);
    expect(rememberDraftAudience(letter, "customer-auto")).toBe(false);
    expect(recallDraftAudience(letter)).toBeNull();
  });

  it("點過一次之後，同一份草稿直接帶著那個選項", () => {
    expect(rememberDraftAudience(letter, "manager")).toBe(true);
    const prepared = prepareDraftSend(letter);
    expect(prepared.send).toBe(true);
    expect(prepared.text).toBe(`${letter}\n\n【這是寫給誰的】給主管`);
    expect(prepared.text).not.toMatch(/【這是寫給誰的】給客戶/);
  });

  it("換一句話就當成另一份草稿，要再問", () => {
    rememberDraftAudience(letter, "team");
    const other = "起草一則公告，告訴大家大廳整修";
    expect(prepareDraftSend(other)).toEqual({ send: false, text: other });
  });

  it("保留原文換行，只在尾端補上選過的對象", () => {
    const text = "幫我寫一封信\n\n第一段\n第二段";
    rememberDraftAudience(text, "handler");
    expect(prepareDraftSend(text).text).toBe(`${text}\n\n【這是寫給誰的】給承辦`);
  });

  it("草稿上已經寫著選過的標記時，沿用標記，不另外挑", () => {
    const marked = applyAudienceChoice(letter, "team");
    resetDraftAudienceMemory();
    const prepared = prepareDraftSend(marked);
    expect(prepared.send).toBe(true);
    expect(prepared.text).toBe(marked);
  });

  it("這一頁記的選擇只存選項 id，不存草稿原文", () => {
    const secret = "幫我寫一封信，內容是示範用的草稿句子";
    rememberDraftAudience(secret, "customer");
    let blob = "";
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      blob += `${key}=${sessionStorage.getItem(key)}`;
    }
    expect(blob).not.toContain(secret);
    expect(blob).toContain("customer");
  });

  it("重新載入這一頁後，同一份草稿還記得", () => {
    rememberDraftAudience(letter, "handler");
    resetDraftAudienceMemory({ session: false });
    expect(recallDraftAudience(letter)).toBe("handler");
    expect(prepareDraftSend(letter).text).toMatch(/【這是寫給誰的】給承辦$/);
  });
});
