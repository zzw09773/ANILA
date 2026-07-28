// 輸入框指令解析 —— 觸發字元分派、`@` 零回歸、斜線指令清單與送出時比對。
import { describe, it, expect } from "vitest";

import {
  COMMAND_KINDS,
  agentCandidates,
  buildSlashCommands,
  filterSlashCommands,
  matchSubmitCommand,
  parseComposerTrigger,
} from "../commands/slashCommands.js";
import { DEFAULT_MESSAGE_ACTIONS } from "../commands/promptActions.js";

// 重構前 chat.jsx 內嵌的 `@` 規則,用來確認行為逐字保留。
const LEGACY_MENTION_RE = /(?:^|[\s(])@([\S]*)$/;
const legacyMentionQuery = (text, caret) => {
  const m = text.slice(0, caret).match(LEGACY_MENTION_RE);
  return m ? m[1] : null;
};

describe("parseComposerTrigger — @ (零回歸)", () => {
  const cases = [
    "@",
    "@ra",
    "hello @ra",
    "hello @",
    "(@rag",
    "多行\n第二行 @ra",
    "no trigger here",
    "mail me at foo@bar.com",
    "@rag done ",
  ];

  it("matches the pre-refactor regex on every legacy case", () => {
    for (const text of cases) {
      const caret = text.length;
      const legacy = legacyMentionQuery(text, caret);
      const parsed = parseComposerTrigger(text, caret);
      const got = parsed?.char === "@" ? parsed.query : null;
      expect(got, `case: ${JSON.stringify(text)}`).toBe(legacy);
    }
  });

  it("only looks at text before the caret", () => {
    expect(parseComposerTrigger("@rag tail", 4)).toEqual({ char: "@", query: "rag" });
    expect(parseComposerTrigger("@rag tail", 9)).toBeNull();
  });

  it("returns the empty query right after a bare @", () => {
    expect(parseComposerTrigger("問問 @", 4)).toEqual({ char: "@", query: "" });
  });
});

describe("parseComposerTrigger — /", () => {
  it("triggers only at the very start of the input", () => {
    expect(parseComposerTrigger("/", 1)).toEqual({ char: "/", query: "" });
    expect(parseComposerTrigger("/翻", 2)).toEqual({ char: "/", query: "翻" });
  });

  it("does not trigger mid-text or after a space (避免 URL / 一般文字誤觸)", () => {
    expect(parseComposerTrigger("看 https://x/y", 13)).toBeNull();
    expect(parseComposerTrigger("a/b", 3)).toBeNull();
    expect(parseComposerTrigger("/翻譯 一段話", 7)).toBeNull();
    expect(parseComposerTrigger("第一行\n/翻譯", 8)).toBeNull();
  });

  it("gives @ precedence so mentions never regress", () => {
    expect(parseComposerTrigger("@x", 2)?.char).toBe("@");
  });

  it("is defensive about bad input", () => {
    expect(parseComposerTrigger(undefined, 3)).toBeNull();
    expect(parseComposerTrigger("/x", undefined)).toBeNull();
  });

  // 只看 caret 前面的文字不夠:游標移回指令尾端時 before 仍是 "/摘要",
  // 選單會重開、Enter 直接執行指令並丟掉已經打好的參數。
  it("caret 後面已經有參數時不觸發(游標移回指令尾端)", () => {
    expect(parseComposerTrigger("/摘要 參數", 3)).toBeNull();
    expect(parseComposerTrigger("/翻譯 這段話", 3)).toBeNull();
    expect(parseComposerTrigger("/清空\n第二行", 3)).toBeNull();
  });

  it("caret 後面只有空白時仍算「還在打這個指令」", () => {
    expect(parseComposerTrigger("/摘要", 3)).toEqual({ char: "/", query: "摘要" });
    expect(parseComposerTrigger("/摘要  ", 3)).toEqual({ char: "/", query: "摘要" });
    expect(parseComposerTrigger("/摘要\n", 3)).toEqual({ char: "/", query: "摘要" });
  });
});

describe("agentCandidates", () => {
  const agents = [
    { id: "anila-router", name: "ANILA Router", short: "auto" },
    { id: "rag-agent", name: "知識檢索", short: "rag" },
    { id: "ocr-agent", name: "文件辨識", short: "ocr" },
  ];

  it("excludes the router pseudo-agent", () => {
    expect(agentCandidates(agents, "").map((a) => a.id)).toEqual(["rag-agent", "ocr-agent"]);
  });

  it("matches on id / name / short, case-insensitively", () => {
    expect(agentCandidates(agents, "RA").map((a) => a.id)).toEqual(["rag-agent"]);
    expect(agentCandidates(agents, "辨識").map((a) => a.id)).toEqual(["ocr-agent"]);
  });

  it("returns nothing for a null query", () => {
    expect(agentCandidates(agents, null)).toEqual([]);
  });
});

describe("buildSlashCommands", () => {
  it("always ships /翻譯 /摘要 /公文 /清空 /快捷鍵 /搜尋", () => {
    const names = buildSlashCommands().map((c) => c.name);
    for (const expected of ["翻譯", "摘要", "公文", "清空", "快捷鍵", "搜尋"]) {
      expect(names).toContain(expected);
    }
  });

  it("reuses the existing quick-action templates (不重造)", () => {
    const translate = buildSlashCommands().find((c) => c.name === "翻譯");
    const source = DEFAULT_MESSAGE_ACTIONS.find((a) => a.id === "translate-en");
    expect(translate.kind).toBe(COMMAND_KINDS.PROMPT_ACTION);
    expect(translate.template).toBe(source.config.template);
    expect(translate.template).toContain("{content}");
  });

  it("surfaces CSP-defined prompt_action functions as commands", () => {
    const commands = buildSlashCommands({
      actions: [{ id: "legalese", label: "法務用語", config: { template: "改寫：{content}" } }],
    });
    const custom = commands.find((c) => c.name === "法務用語");
    expect(custom).toBeTruthy();
    expect(custom.template).toBe("改寫：{content}");
    // 內建三項仍以預設模板補齊
    expect(commands.map((c) => c.name)).toEqual(expect.arrayContaining(["翻譯", "摘要", "公文"]));
  });

  it("drops every quick-action command for classified conversations", () => {
    const commands = buildSlashCommands({ classified: true });
    expect(commands.some((c) => c.kind === COMMAND_KINDS.PROMPT_ACTION)).toBe(false);
    expect(commands.map((c) => c.name)).toEqual(["清空", "快捷鍵", "搜尋"]);
  });

  it("skips actions without a usable template", () => {
    const commands = buildSlashCommands({ actions: [{ id: "x", label: "空的" }] });
    expect(commands.some((c) => c.name === "空的")).toBe(false);
  });
});

describe("filterSlashCommands", () => {
  const commands = buildSlashCommands();

  it("returns a bounded list for the empty query", () => {
    expect(filterSlashCommands(commands, "").length).toBeLessThanOrEqual(8);
    expect(filterSlashCommands(commands, "").length).toBeGreaterThan(0);
  });

  it("ranks exact match above prefix above substring", () => {
    expect(filterSlashCommands(commands, "翻譯")[0].name).toBe("翻譯");
    expect(filterSlashCommands(commands, "清")[0].name).toBe("清空");
  });

  it("matches latin aliases", () => {
    expect(filterSlashCommands(commands, "clear")[0].name).toBe("清空");
    expect(filterSlashCommands(commands, "shortcuts")[0].name).toBe("快捷鍵");
  });

  it("returns nothing for an unknown query", () => {
    expect(filterSlashCommands(commands, "zzzz")).toEqual([]);
  });
});

describe("matchSubmitCommand", () => {
  const commands = buildSlashCommands();

  it("captures `/指令 參數`", () => {
    const hit = matchSubmitCommand("/翻譯 這段話要翻", commands);
    expect(hit.command.name).toBe("翻譯");
    expect(hit.arg).toBe("這段話要翻");
  });

  it("captures a bare `/指令` with no argument", () => {
    expect(matchSubmitCommand("/清空", commands).arg).toBe("");
  });

  it("keeps multi-line arguments intact", () => {
    expect(matchSubmitCommand("/摘要 第一行\n第二行", commands).arg).toBe("第一行\n第二行");
  });

  it("ignores non-command text so normal messages still send", () => {
    expect(matchSubmitCommand("一般訊息", commands)).toBeNull();
    expect(matchSubmitCommand("/不存在的指令 x", commands)).toBeNull();
    expect(matchSubmitCommand("/", commands)).toBeNull();
    expect(matchSubmitCommand("看 /usr/local 這個路徑", commands)).toBeNull();
  });

  it("does not resolve quick-action commands when classified", () => {
    const locked = buildSlashCommands({ classified: true });
    expect(matchSubmitCommand("/翻譯 機密內容", locked)).toBeNull();
  });

  // 指令必須是輸入的**第一個字元**。呼叫端先 trim 再進來的話,從別處貼上的
  // 「  /翻譯 機密內容」會被當指令執行 —— 使用者本意是送出那段文字。
  it("前置空白 / 換行的 /指令 不算指令", () => {
    expect(matchSubmitCommand(" /翻譯 這段話", commands)).toBeNull();
    expect(matchSubmitCommand("\n/清空", commands)).toBeNull();
    expect(matchSubmitCommand("\t/摘要 x", commands)).toBeNull();
    expect(matchSubmitCommand("　/摘要 x", commands)).toBeNull(); // 全形空白
  });
});
