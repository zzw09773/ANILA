import { describe, it, expect } from "vitest";
import { cleanGeneratedTitle } from "../runtime/titleClean.js";

describe("cleanGeneratedTitle - placeholder guard", () => {
  it("rejects Router fallback placeholder echoed as title", () => {
    const raw =
      "（Router 已完成分析但未能自動萃取最終回覆，請展開上方「思考過程」檢視。）";
    expect(cleanGeneratedTitle(raw)).toBeNull();
  });

  it("rejects partial placeholder echoes", () => {
    expect(cleanGeneratedTitle("Router 已完成分析")).toBeNull();
    expect(cleanGeneratedTitle("未能自動萃取的回覆")).toBeNull();
    expect(cleanGeneratedTitle("思考過程說明")).toBeNull();
  });
});

describe("cleanGeneratedTitle - basic cleanup", () => {
  it("strips wrapping quotes", () => {
    expect(cleanGeneratedTitle("「特休計算」")).toBe("特休計算");
    expect(cleanGeneratedTitle('"Expense Report Flow"')).toBe("Expense Report Flow");
  });

  it("strips trailing punctuation", () => {
    expect(cleanGeneratedTitle("特休怎麼計算？")).toBe("特休怎麼計算");
    expect(cleanGeneratedTitle("出差報銷流程。")).toBe("出差報銷流程");
  });

  it("clamps to 30 chars", () => {
    const long = "這是一個非常非常非常非常非常非常非常非常長的標題，超過三十字的情況";
    expect(cleanGeneratedTitle(long).length).toBe(30);
  });
});

describe("cleanGeneratedTitle - rejects too-short output", () => {
  it("returns null for 0-1 meaningful chars", () => {
    expect(cleanGeneratedTitle("")).toBeNull();
    expect(cleanGeneratedTitle("   ")).toBeNull();
    expect(cleanGeneratedTitle("？")).toBeNull();
    expect(cleanGeneratedTitle("A")).toBeNull();
  });

  it("accepts exactly 2 chars", () => {
    expect(cleanGeneratedTitle("特休")).toBe("特休");
  });
});

describe("cleanGeneratedTitle - non-string input", () => {
  it("returns null for non-string", () => {
    expect(cleanGeneratedTitle(null)).toBeNull();
    expect(cleanGeneratedTitle(undefined)).toBeNull();
    expect(cleanGeneratedTitle(42)).toBeNull();
    expect(cleanGeneratedTitle({})).toBeNull();
  });
});

describe("cleanGeneratedTitle - collapses self-repeat", () => {
  it("collapses AA pattern (no separator)", () => {
    expect(cleanGeneratedTitle("軍人規定軍人規定")).toBe("軍人規定");
  });

  it("collapses AA pattern with whitespace separator", () => {
    expect(cleanGeneratedTitle("軍人規定 軍人規定")).toBe("軍人規定");
  });

  it("collapses AA pattern with newline", () => {
    expect(cleanGeneratedTitle("行政處分申訴程序\n行政處分申訴程序")).toBe(
      "行政處分申訴程序",
    );
  });

  it("leaves non-repeating titles alone", () => {
    expect(cleanGeneratedTitle("特休計算方法")).toBe("特休計算方法");
  });

  it("does not over-collapse odd-length strings", () => {
    // "ABCABCA" should NOT collapse (length 7, not a clean AA split)
    expect(cleanGeneratedTitle("特休特休特")).toBe("特休特休特");
  });
});

describe("cleanGeneratedTitle - legitimate titles pass", () => {
  it("preserves a normal summary", () => {
    expect(cleanGeneratedTitle("特休天數計算方式")).toBe("特休天數計算方式");
  });

  it("handles mixed punctuation + quotes + content", () => {
    expect(cleanGeneratedTitle("「FastAPI SSE 串流代理」。")).toBe(
      "FastAPI SSE 串流代理",
    );
  });
});
