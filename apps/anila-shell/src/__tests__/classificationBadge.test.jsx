import { describe, it, expect } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import React from "react";
import { ClassificationLevelBadge } from "../trust.jsx";
import {
  classificationLevelBadge,
  CLASSIFICATION_LEVELS,
} from "../runtime/classified.js";

afterEach(cleanup);

// The five-level badge is additive to the boolean「密等鎖定」(latch) indicator.
// It renders the zh-TW level text for any elevated level, nothing for the
// floor 無機密, and nothing when the field is absent (boolean-only payload).

describe("classificationLevelBadge (pure)", () => {
  it("returns the label for each elevated level", () => {
    for (const level of CLASSIFICATION_LEVELS.filter((l) => l !== "無機密")) {
      expect(classificationLevelBadge({ classification_level: level })).toBe(level);
    }
  });

  it("returns null for the floor 無機密", () => {
    expect(classificationLevelBadge({ classification_level: "無機密" })).toBeNull();
  });

  it("returns null when the field is absent (boolean-only fallback)", () => {
    expect(classificationLevelBadge({ classified: true })).toBeNull();
    expect(classificationLevelBadge({})).toBeNull();
    expect(classificationLevelBadge(null)).toBeNull();
  });

  it("returns null for an unrecognised level string", () => {
    expect(classificationLevelBadge({ classification_level: "絕密" })).toBeNull();
  });

  it("honours the camelCase mapped field too", () => {
    expect(classificationLevelBadge({ classificationLevel: "極機密" })).toBe("極機密");
  });
});

describe("<ClassificationLevelBadge>", () => {
  it("renders the level text as a badge per elevated level", () => {
    for (const level of ["營業秘密", "機密", "極機密", "絕對機密"]) {
      const { container } = render(
        <ClassificationLevelBadge conversation={{ classification_level: level }} />,
      );
      expect(container.textContent).toContain(level);
      cleanup();
    }
  });

  it("renders nothing for 無機密", () => {
    const { container } = render(
      <ClassificationLevelBadge conversation={{ classification_level: "無機密" }} />,
    );
    expect(container.textContent.trim()).toBe("");
  });

  it("renders nothing when classification_level is absent (falls back)", () => {
    const { container } = render(
      <ClassificationLevelBadge conversation={{ classified: true }} />,
    );
    expect(container.textContent.trim()).toBe("");
  });
});
