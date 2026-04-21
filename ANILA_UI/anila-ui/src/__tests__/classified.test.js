import { describe, it, expect } from "vitest";
import {
  computeConversationClassified,
  appendClassifiedTag,
  latchConversationWithMeta,
} from "../runtime/classified.js";

// Security invariant: classified is a ONE-WAY latch.
// These tests pin that invariant for regression protection.

describe("computeConversationClassified", () => {
  it("is false when no signal present", () => {
    expect(computeConversationClassified({ classified: false }, {})).toBe(false);
  });

  it("stays true when conversation already classified", () => {
    expect(
      computeConversationClassified(
        { classified: true },
        { agentRequiresEncryption: false, metaClassified: false },
      ),
    ).toBe(true);
  });

  it("upgrades when agent requires encryption", () => {
    expect(
      computeConversationClassified(
        { classified: false },
        { agentRequiresEncryption: true },
      ),
    ).toBe(true);
  });

  it("upgrades when meta reports classified=true", () => {
    expect(
      computeConversationClassified(
        { classified: false },
        { metaClassified: true },
      ),
    ).toBe(true);
  });

  it("does NOT downgrade when prior=true + agent=false + meta=false", () => {
    expect(
      computeConversationClassified(
        { classified: true },
        { agentRequiresEncryption: false, metaClassified: false },
      ),
    ).toBe(true);
  });

  it("treats meta.classified=false as 'no signal' (never negative latch)", () => {
    // Per the Wave B plan: a misbehaving agent claiming classified=false on a
    // previously-classified conversation must NOT downgrade.
    expect(
      computeConversationClassified(
        { classified: true },
        { metaClassified: false },
      ),
    ).toBe(true);
  });

  it("handles missing conversation gracefully", () => {
    expect(computeConversationClassified(undefined, { metaClassified: true })).toBe(true);
    expect(computeConversationClassified(null, {})).toBe(false);
  });
});

describe("appendClassifiedTag", () => {
  it("appends classified to empty tags", () => {
    expect(appendClassifiedTag(undefined)).toEqual(["classified"]);
    expect(appendClassifiedTag([])).toEqual(["classified"]);
  });

  it("preserves existing tags + appends classified once", () => {
    expect(appendClassifiedTag(["urgent"])).toEqual(["urgent", "classified"]);
  });

  it("is idempotent when classified is already present", () => {
    expect(appendClassifiedTag(["classified", "x"])).toEqual(["classified", "x"]);
  });
});

describe("latchConversationWithMeta", () => {
  it("returns the same conversation when meta.classified is not true", () => {
    const conv = { id: 1, classified: false, tags: [] };
    expect(latchConversationWithMeta(conv, { classified: false })).toBe(conv);
    expect(latchConversationWithMeta(conv, {})).toBe(conv);
    expect(latchConversationWithMeta(conv, { classified: "truthy-but-not-true" })).toBe(conv);
  });

  it("returns the same conversation when already classified (idempotent)", () => {
    const conv = { id: 1, classified: true, tags: ["classified"] };
    expect(latchConversationWithMeta(conv, { classified: true })).toBe(conv);
  });

  it("flips to classified + appends tag when meta.classified=true", () => {
    const conv = { id: 1, classified: false, tags: ["urgent"] };
    const next = latchConversationWithMeta(conv, { classified: true });
    expect(next).not.toBe(conv); // new object
    expect(next.classified).toBe(true);
    expect(next.tags).toEqual(["urgent", "classified"]);
  });

  it("does not mutate the input conversation", () => {
    const conv = { id: 1, classified: false, tags: ["a"] };
    latchConversationWithMeta(conv, { classified: true });
    expect(conv.classified).toBe(false);
    expect(conv.tags).toEqual(["a"]);
  });
});
