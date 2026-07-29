import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";
import { ClassificationWatermark, ConfidentialWatermark, watermarkLevel } from "../trust.jsx";

afterEach(cleanup);

// The corner watermark reflects the REAL four-level classification (Slice 3),
// replacing the old decorative English "CONFIDENTIAL". Only 密 and above
// render a full watermark; 營業秘密/無機密/absent render nothing. When the
// classification_level field is absent but the boolean latch is set, it falls
// back to the floor 密 — never English, never the higher 機密.

describe("watermarkLevel (pure)", () => {
  it("returns the elevated level for each machine-sensitive level", () => {
    for (const level of ["密", "機密"]) {
      expect(watermarkLevel({ classificationLevel: level })).toBe(level);
    }
  });

  it("honours the snake_case classification_level field too", () => {
    expect(watermarkLevel({ classification_level: "密" })).toBe("密");
  });

  it("returns null for 無機密 and 營業秘密 (no full watermark)", () => {
    expect(watermarkLevel({ classificationLevel: "無機密" })).toBeNull();
    expect(watermarkLevel({ classificationLevel: "營業秘密" })).toBeNull();
  });

  it("returns null when nothing indicates classification", () => {
    expect(watermarkLevel({})).toBeNull();
    expect(watermarkLevel(null)).toBeNull();
    expect(watermarkLevel(undefined)).toBeNull();
    expect(watermarkLevel({ classified: false })).toBeNull();
  });

  it("falls back to the floor 密 when only the boolean latch is set", () => {
    const result = watermarkLevel({ classified: true });
    expect(result).toBe("密");
    expect(result).not.toBe("CONFIDENTIAL");
    expect(result).not.toBe("機密");
  });

  it("prefers an explicit elevated level over the boolean fallback", () => {
    expect(watermarkLevel({ classificationLevel: "機密", classified: true })).toBe("機密");
  });
});

describe("<ClassificationWatermark>", () => {
  const cases = [
    ["密", "warn"],
    ["機密", "danger-strong"],
  ];

  it("renders the real zh-TW level text with the correct severity class", () => {
    for (const [level, severity] of cases) {
      const { container } = render(<ClassificationWatermark level={level} />);
      const el = container.firstChild;
      expect(el).not.toBeNull();
      expect(el.textContent).toBe(level);
      expect(el.getAttribute("data-severity")).toBe(severity);
      expect(el.className).toContain(`anila-classification-${severity}`);
      cleanup();
    }
  });

  it("never renders the old English CONFIDENTIAL marker", () => {
    for (const [level] of cases) {
      const { container } = render(<ClassificationWatermark level={level} />);
      expect(container.textContent).not.toContain("CONFIDENTIAL");
      cleanup();
    }
  });

  it("renders nothing for 無機密, 營業秘密, or an unknown/absent level", () => {
    for (const level of ["無機密", "營業秘密", "絕密", "", undefined, null]) {
      const { container } = render(<ClassificationWatermark level={level} />);
      expect(container.textContent.trim()).toBe("");
      cleanup();
    }
  });
});

describe("<ConfidentialWatermark>", () => {
  it("falls back to the floor 密 (not 機密) when level is absent", () => {
    const { container } = render(
      <ConfidentialWatermark userEmail="u@example.com" traceId="t-1" />
    );
    expect(container.textContent).toContain("密");
    expect(container.textContent).not.toBe("機密");
    expect(container.textContent).not.toContain("機密");
  });
});
