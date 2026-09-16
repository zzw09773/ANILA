import { describe, it, expect } from "vitest";
import {
  buildArtifactRevisePrompt,
  sourceSelectionText,
} from "../runtime/artifactRevise.js";

describe("buildArtifactRevisePrompt", () => {
  it("returns null without a selection or instruction", () => {
    expect(buildArtifactRevisePrompt({
      kind: "svg",
      source: "<svg/>",
      selected: "",
      instruction: "變藍",
    })).toBeNull();
    expect(buildArtifactRevisePrompt({
      kind: "svg",
      source: "<svg/>",
      selected: "<svg/>",
      instruction: "  ",
    })).toBeNull();
  });

  it("asks for a full rewrite with the excerpt and instruction", () => {
    const prompt = buildArtifactRevisePrompt({
      kind: "html",
      language: "html",
      source: "<html><h1>舊</h1></html>",
      selected: "<h1>舊</h1>",
      instruction: "標題改成新",
    });
    expect(prompt).toContain("改法：標題改成新");
    expect(prompt).toContain("<h1>舊</h1>");
    expect(prompt).toContain("<html><h1>舊</h1></html>");
    expect(prompt).toContain("```html");
    expect(prompt).toContain("完整更新後的產物");
    expect(prompt).toContain("前後不要解釋");
    expect(prompt).toMatch(/比產物內最長的連續反引號多至少一個/);
  });

  it("uses a longer fence when the excerpt or source already contains ```", () => {
    const inner = "```js\nalert(1)\n```";
    const source = `# 範例\n\n${inner}\n`;
    const prompt = buildArtifactRevisePrompt({
      kind: "markdown",
      language: "markdown",
      source,
      selected: inner,
      instruction: "拿掉 alert",
    });
    expect(prompt).toContain("````markdown");
    expect(prompt).toMatch(
      /【完整產物】\n````markdown\n[\s\S]*```js\nalert\(1\)\n```[\s\S]*\n````\n\n請回完整更新後的產物/,
    );
    expect(prompt).toMatch(
      /【要改的片段】\n````markdown\n```js\nalert\(1\)\n```\n````/,
    );
  });

  it("keeps selected indentation instead of trimming the excerpt", () => {
    const prompt = buildArtifactRevisePrompt({
      kind: "html",
      language: "html",
      source: "<div>\n  <p>x</p>\n</div>",
      selected: "  <p>x</p>",
      instruction: "改成 y",
    });
    expect(prompt).toContain("  <p>x</p>");
    expect(prompt).not.toMatch(/【要改的片段】\n```html\n<p>x<\/p>/);
  });
});

describe("sourceSelectionText", () => {
  function fakeSelection({ anchor, focus, ancestor, text, rangeCount = 1 }) {
    return {
      isCollapsed: false,
      toString: () => text,
      anchorNode: anchor,
      focusNode: focus,
      rangeCount,
      getRangeAt: () => ({ commonAncestorContainer: ancestor }),
    };
  }

  it("returns the raw text when the whole range is inside the root", () => {
    const root = { contains: (n) => n === "in" || n === root };
    const sel = fakeSelection({
      anchor: "in",
      focus: "in",
      ancestor: "in",
      text: "  indented",
    });
    expect(sourceSelectionText(sel, root)).toBe("  indented");
  });

  it("rejects a selection that only has one end inside the root", () => {
    const root = { contains: (n) => n === "in" };
    const sel = fakeSelection({
      anchor: "in",
      focus: "out",
      ancestor: "body",
      text: "crossed",
    });
    expect(sourceSelectionText(sel, root)).toBeNull();
  });

  it("rejects a selection whose common ancestor is outside the root", () => {
    const root = { contains: (n) => n === "in" };
    const sel = fakeSelection({
      anchor: "in",
      focus: "in",
      ancestor: "body",
      text: "inside-looking",
    });
    expect(sourceSelectionText(sel, root)).toBeNull();
  });
});
