import { describe, it, expect } from "vitest";
import { STREAM_STATE } from "../runtime/reservedTurn.js";
import {
  canConsumeRevisionTurn,
  extractRevisionCandidate,
  shouldApplyArtifactFence,
} from "../runtime/artifactRevision.js";

const current = { kind: "html", source: "<h1>舊</h1>" };
const ticket = { messageId: "a-new", kind: "html" };

describe("shouldApplyArtifactFence", () => {
  it("updates a streaming prefix without consuming the ticket", () => {
    expect(shouldApplyArtifactFence({
      ticket,
      fenceMessageId: "a-new",
      streaming: true,
      artifactKind: "html",
      source: "<h1>舊</h1><p>x</p>",
      current,
    })).toEqual({ apply: true, consume: false });
  });

  it("does not consume a replacement while the turn is still streaming", () => {
    expect(shouldApplyArtifactFence({
      ticket,
      fenceMessageId: "a-new",
      streaming: true,
      artifactKind: "html",
      source: "<h1>新</h1>",
      current,
    })).toEqual({ apply: false, consume: false });
  });

  it("consumes a same-kind replacement only on the ticket message after the turn ends", () => {
    expect(shouldApplyArtifactFence({
      ticket,
      fenceMessageId: "a-new",
      streaming: false,
      artifactKind: "html",
      source: "<h1>新</h1>",
      current,
    })).toEqual({ apply: true, consume: true });
  });

  it("ignores a historical fence even when a ticket is pending", () => {
    expect(shouldApplyArtifactFence({
      ticket,
      fenceMessageId: "a-old",
      streaming: false,
      artifactKind: "html",
      source: "<h1>歷史</h1>",
      current,
    })).toEqual({ apply: false, consume: false });
  });

  it("ignores a replacement when there is no ticket", () => {
    expect(shouldApplyArtifactFence({
      ticket: null,
      fenceMessageId: "a-new",
      streaming: false,
      artifactKind: "html",
      source: "<h1>新</h1>",
      current,
    })).toEqual({ apply: false, consume: false });
  });

  it("ignores a wrong-kind fence on the ticket message", () => {
    expect(shouldApplyArtifactFence({
      ticket,
      fenceMessageId: "a-new",
      streaming: false,
      artifactKind: "svg",
      source: "<svg/>",
      current,
    })).toEqual({ apply: false, consume: false });
  });

  it("does not let a historical prefix grow the open artifact while a ticket is live", () => {
    expect(shouldApplyArtifactFence({
      ticket,
      fenceMessageId: "a-old",
      streaming: false,
      artifactKind: "html",
      source: "<h1>舊</h1><p>歷史加長</p>",
      current,
    })).toEqual({ apply: false, consume: false });
  });

  it("does not apply a same-message prefix when the ticket kind differs", () => {
    expect(shouldApplyArtifactFence({
      ticket: { messageId: "a-new", kind: "html" },
      fenceMessageId: "a-new",
      streaming: false,
      artifactKind: "svg",
      source: "<svg xmlns='http://www.w3.org/2000/svg'><circle/></svg>",
      current: { kind: "svg", source: "<svg xmlns='http://www.w3.org/2000/svg'>" },
    })).toEqual({ apply: false, consume: false });
  });

  it("does not consume a replacement after stop, failure, or length truncation", () => {
    const partial = {
      ticket,
      fenceMessageId: "a-new",
      streaming: false,
      artifactKind: "html",
      source: "<h1>半",
      current,
    };
    expect(shouldApplyArtifactFence({
      ...partial,
      streamState: STREAM_STATE.STOPPED,
    })).toEqual({ apply: false, consume: false });
    expect(shouldApplyArtifactFence({
      ...partial,
      streamState: STREAM_STATE.FAILED,
    })).toEqual({ apply: false, consume: false });
    expect(shouldApplyArtifactFence({
      ...partial,
      streamState: STREAM_STATE.COMPLETE,
      finishReason: "length",
    })).toEqual({ apply: false, consume: false });
  });
});

describe("canConsumeRevisionTurn", () => {
  it("allows a finished complete turn and rejects incomplete terminals", () => {
    expect(canConsumeRevisionTurn({ streaming: false })).toBe(true);
    expect(canConsumeRevisionTurn({ streaming: false, streamState: STREAM_STATE.COMPLETE })).toBe(true);
    expect(canConsumeRevisionTurn({ streaming: true, streamState: STREAM_STATE.COMPLETE })).toBe(false);
    expect(canConsumeRevisionTurn({ streaming: false, streamState: STREAM_STATE.STOPPED })).toBe(false);
    expect(canConsumeRevisionTurn({ streaming: false, streamState: STREAM_STATE.FAILED })).toBe(false);
    expect(canConsumeRevisionTurn({ streaming: false, streamState: STREAM_STATE.INTERRUPTED })).toBe(false);
    expect(canConsumeRevisionTurn({
      streaming: false,
      streamState: STREAM_STATE.COMPLETE,
      finishReason: "length",
    })).toBe(false);
  });
});

describe("extractRevisionCandidate", () => {
  it("keeps nested triple-backtick code inside a markdown artifact", () => {
    const text = [
      "```markdown",
      "# 範例",
      "",
      "```js",
      "alert(1)",
      "```",
      "```",
    ].join("\n");
    const picked = extractRevisionCandidate(text, "markdown");
    expect(picked?.source).toContain("```js");
    expect(picked?.source).toContain("alert(1)");
    expect(picked?.source).toContain("# 範例");
  });

  it("picks the longer of two sibling html fences", () => {
    const text = "```html\n<p>短</p>\n```\n\n```html\n<h1>完整新產物</h1>\n```";
    const picked = extractRevisionCandidate(text, "html");
    expect(picked?.source).toContain("完整新產物");
    expect(picked?.source).not.toContain("短");
  });

  it("does not swallow a following svg sibling or the prose between fences", () => {
    const text = [
      "說明如下",
      "",
      "```html",
      "<h1>新</h1>",
      "```",
      "",
      "再補一段",
      "```svg",
      "<svg xmlns='http://www.w3.org/2000/svg'><circle/></svg>",
      "```",
    ].join("\n");
    const picked = extractRevisionCandidate(text, "html");
    expect(picked?.source.trim()).toBe("<h1>新</h1>");
    expect(picked?.source).not.toContain("再補一段");
    expect(picked?.source).not.toContain("<svg");
  });

  it("does not swallow a following html sibling into a markdown candidate", () => {
    const text = [
      "```markdown",
      "# 說明",
      "",
      "```js",
      "alert(1)",
      "```",
      "```",
      "",
      "```html",
      "<h1>另一份</h1>",
      "```",
    ].join("\n");
    const picked = extractRevisionCandidate(text, "markdown");
    expect(picked?.source).toContain("# 說明");
    expect(picked?.source).toContain("```js");
    expect(picked?.source).not.toContain("另一份");
  });

  it("does not swallow prose or a following svg when the first html fence never closes", () => {
    const text = [
      "```html",
      "<h1>新</h1>",
      "",
      "再補一段",
      "```svg",
      "<svg xmlns='http://www.w3.org/2000/svg'><circle/></svg>",
      "```",
    ].join("\n");
    const picked = extractRevisionCandidate(text, "html");
    expect(picked).toBeNull();
  });
});
