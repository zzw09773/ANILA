import { describe, it, expect } from "vitest";
import { expandTerm, matchFuzzy } from "../runtime/searchSynonyms.js";

describe("expandTerm", () => {
  it("returns the token itself for unknown words", () => {
    expect(expandTerm("FooBar")).toEqual(["foobar"]);
  });

  it("returns the full synonym group for known tokens", () => {
    const out = expandTerm("特休");
    expect(out).toContain("特休");
    expect(out).toContain("年假");
    expect(out).toContain("hr");
  });

  it("is case-insensitive on the input", () => {
    const out = expandTerm("SSE");
    expect(out).toContain("sse");
    expect(out).toContain("fastapi");
  });

  it("unions groups when a token sits in multiple groups", () => {
    // "hr" lives in both 特休 and 加班 groups → expansion contains both sets
    const out = expandTerm("hr");
    expect(out).toContain("特休");
    expect(out).toContain("加班");
  });
});

describe("matchFuzzy", () => {
  const conv = (title, tags = []) => ({ title, tags });

  it("returns true on empty query", () => {
    expect(matchFuzzy(conv("任何"), "")).toBe(true);
    expect(matchFuzzy(conv("任何"), "   ")).toBe(true);
  });

  it("matches literal substring in title", () => {
    expect(matchFuzzy(conv("特休怎麼算"), "特休")).toBe(true);
  });

  it("expands to synonyms when query differs from title", () => {
    // user types "特休", conversation title is "年假規定" — should still match
    expect(matchFuzzy(conv("年假規定"), "特休")).toBe(true);
  });

  it("matches tag when neither literal nor synonym is in title", () => {
    expect(matchFuzzy(conv("規定", ["HR"]), "特休")).toBe(true);
  });

  it("ANDs multiple tokens", () => {
    expect(matchFuzzy(conv("特休規定 2025"), "特休 2025")).toBe(true);
    expect(matchFuzzy(conv("特休規定"), "特休 出差")).toBe(false);
  });

  it("returns false when no synonym hits", () => {
    expect(matchFuzzy(conv("無關主題"), "特休")).toBe(false);
  });
});
