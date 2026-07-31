import { describe, it, expect } from "vitest";
import { parseMentions, findMentions } from "../multiagent.jsx";

// FIX 4: the mention regex used to be /@([a-z][\w-]*)/gi, so a CJK agent
// name could never match. `@軍人法規智慧助手` looked to the user like an
// explicit choice, then silently fell back to a full LLM routing pass —
// the user believed they chose, and paid for a routing decision anyway.

const AGENTS = [
  { id: "mil-law", short: "millaw", name: "軍人法規智慧助手" },
  { id: "asrd", short: "asrd", name: "ASRD" },
  { id: "anila-router", short: "router", name: "ANILA Router" },
];

describe("parseMentions - non-ASCII agent names", () => {
  it("recognises a Chinese agent name and bypasses LLM routing", () => {
    const { explicitAgents } = parseMentions("@軍人法規智慧助手 請問服役年限", AGENTS);
    expect(explicitAgents).toEqual(["mil-law"]);
    // app.jsx:1312 does `explicitAgents[0] || selectedAgentId` — resolving to
    // a real agent id is exactly what skips the router (and its routing pass).
    expect(explicitAgents[0]).not.toBe("anila-router");
  });

  it("recognises a Chinese name mid-sentence after whitespace", () => {
    const { explicitAgents } = parseMentions("先幫我問 @軍人法規智慧助手 這題", AGENTS);
    expect(explicitAgents).toEqual(["mil-law"]);
  });

  it("still recognises ASCII id and short forms, case-insensitively", () => {
    expect(parseMentions("@ASRD hello", AGENTS).explicitAgents).toEqual(["asrd"]);
    expect(parseMentions("@millaw hi", AGENTS).explicitAgents).toEqual(["mil-law"]);
  });

  it("dedupes repeated mentions and keeps first-seen order", () => {
    const { explicitAgents } = parseMentions(
      "@asrd 跟 @軍人法規智慧助手 還有 @asrd",
      AGENTS,
    );
    expect(explicitAgents).toEqual(["asrd", "mil-law"]);
  });
});

describe("parseMentions - false positives", () => {
  it("does not read an email address as a mention", () => {
    expect(parseMentions("寄到 asrd@asrd.example.com 好嗎", AGENTS).explicitAgents)
      .toEqual([]);
    expect(parseMentions("user@軍人法規智慧助手.tw", AGENTS).explicitAgents)
      .toEqual([]);
  });

  it("ignores a stray @ and unknown handles", () => {
    expect(parseMentions("@ 這是什麼 @nobody @2024", AGENTS).explicitAgents)
      .toEqual([]);
  });

  it("returns the original text untouched", () => {
    const text = "@軍人法規智慧助手 請問服役年限";
    expect(parseMentions(text, AGENTS).content).toBe(text);
  });
});

describe("findMentions", () => {
  it("reports the matched span so highlighting covers the whole name", () => {
    const hits = findMentions("問 @軍人法規智慧助手 好嗎", AGENTS);
    expect(hits).toHaveLength(1);
    expect(hits[0].label).toBe("軍人法規智慧助手");
    expect(hits[0].length).toBe("@軍人法規智慧助手".length);
  });

  it("prefers the longest matching label", () => {
    const agents = [
      { id: "a", name: "Agent" },
      { id: "ab", name: "Agent Beta" },
    ];
    expect(findMentions("@Agent Beta 你好", agents)[0].agent.id).toBe("ab");
  });
});
