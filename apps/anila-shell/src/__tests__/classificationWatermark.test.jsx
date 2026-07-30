import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup, act } from "@testing-library/react";
import React from "react";
import {
  ClassificationWatermark,
  ConfidentialWatermark,
  watermarkLevel,
  watermarkReaderLabel,
  formatWatermarkMinute,
  buildWatermarkText,
  WATERMARK_DISCLAIMER,
} from "../trust.jsx";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// The corner watermark reflects the REAL four-level classification (Slice 3),
// replacing the old decorative English "CONFIDENTIAL". Only 密 and above
// render a full watermark; 營業秘密/無機密/absent render nothing. When the
// classification_level field is absent but the boolean latch is set, it falls
// back to the floor 密 — never English, never the higher 機密.
//
// P4.2: the full-page ConfidentialWatermark carries level · reader · time
// (minute precision). Reader comes from the signed-in user; time freezes per
// displayed conversation (re-freezes on conversation change, stable while open).

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

describe("watermarkReaderLabel / formatWatermarkMinute (pure)", () => {
  it("prefers email then username from the signed-in user object", () => {
    expect(watermarkReaderLabel({ email: "a@example.com", username: "alice" }))
      .toBe("a@example.com");
    expect(watermarkReaderLabel({ username: "alice" })).toBe("alice");
  });

  it("returns null when identity is unavailable (no misleading placeholder)", () => {
    expect(watermarkReaderLabel(null)).toBeNull();
    expect(watermarkReaderLabel(undefined)).toBeNull();
    expect(watermarkReaderLabel({})).toBeNull();
    expect(watermarkReaderLabel({ email: "  ", username: "" })).toBeNull();
  });

  it("formats a timestamp to the minute, not a live clock tick", () => {
    const stamp = formatWatermarkMinute(new Date(2026, 6, 30, 15, 30, 45));
    expect(stamp).toBe("2026-07-30 15:30");
    // Minute precision only — no seconds field.
    expect(stamp).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(stamp.split(":").length).toBe(2);
  });

  it("builds the three-part forensic line", () => {
    expect(buildWatermarkText({
      level: "密",
      reader: "a@example.com",
      readAt: "2026-07-30 15:30",
    })).toBe("本文件屬密 · a@example.com · 2026-07-30 15:30");
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
  // TEST SEAM ONLY — production never passes readAtForTests.
  const FIXED_AT = "2026-07-30 15:30";
  const READER = "reader@ncsist.org.tw";

  it("shows level, reader, and minute timestamp from props (not hardcoded)", () => {
    const { container } = render(
      <ConfidentialWatermark
        level="密"
        reader={READER}
        conversationId={1}
        readAtForTests={FIXED_AT}
      />
    );
    const expected = buildWatermarkText({ level: "密", reader: READER, readAt: FIXED_AT });
    expect(container.textContent).toContain(expected);
    expect(container.textContent).toContain("密");
    expect(container.textContent).toContain(READER);
    expect(container.textContent).toContain(FIXED_AT);
    // Reader is the prop value, not a baked-in placeholder
    expect(container.textContent).not.toContain("user ·");
    expect(container.textContent).not.toMatch(/\buser\b/);
  });

  it("renders the elevated level literally (機密, not floor 密)", () => {
    const { container } = render(
      <ConfidentialWatermark
        level="機密"
        reader={READER}
        conversationId={1}
        readAtForTests={FIXED_AT}
      />
    );
    expect(container.textContent).toContain("本文件屬機密");
    expect(container.textContent).not.toContain("本文件屬密 ·");
  });

  it("does not appear for content below the watermark threshold", () => {
    for (const level of ["無機密", "營業秘密", "", undefined, null]) {
      const { container } = render(
        <ConfidentialWatermark
          level={level}
          reader={READER}
          conversationId={1}
          readAtForTests={FIXED_AT}
        />
      );
      expect(container.textContent.trim()).toBe("");
      cleanup();
    }
  });

  it("appears for content at and above the threshold (密／機密)", () => {
    for (const level of ["密", "機密"]) {
      const { container } = render(
        <ConfidentialWatermark
          level={level}
          reader={READER}
          conversationId={1}
          readAtForTests={FIXED_AT}
        />
      );
      expect(container.querySelector("[data-watermark='forensic']")).not.toBeNull();
      expect(container.textContent).toContain(`本文件屬${level}`);
      expect(container.textContent).toContain(READER);
      expect(container.textContent).toContain(FIXED_AT);
      cleanup();
    }
  });

  it("renders nothing when the reader identity is not yet known", () => {
    const { container } = render(
      <ConfidentialWatermark
        level="密"
        conversationId={1}
        readAtForTests={FIXED_AT}
      />
    );
    expect(container.textContent.trim()).toBe("");
  });

  it("does not invent a placeholder reader or fall back to misleading 'user'", () => {
    const { container } = render(
      <ConfidentialWatermark
        level="密"
        reader="  "
        conversationId={1}
        readAtForTests={FIXED_AT}
      />
    );
    expect(container.textContent.trim()).toBe("");
    expect(container.textContent).not.toContain("user");
  });

  it("accepts the legacy userEmail prop as the reader source", () => {
    const { container } = render(
      <ConfidentialWatermark
        level="密"
        userEmail={READER}
        conversationId={1}
        readAtForTests={FIXED_AT}
      />
    );
    expect(container.textContent).toContain(READER);
  });

  it("does not block pointer events on the page underneath", () => {
    const { container } = render(
      <ConfidentialWatermark
        level="密"
        reader={READER}
        conversationId={1}
        readAtForTests={FIXED_AT}
      />
    );
    const el = container.querySelector("[data-watermark='forensic']");
    expect(el.style.pointerEvents).toBe("none");
  });

  it("re-freezes the stamp when the displayed conversation changes, stays stable while it does not", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 6, 30, 15, 30, 0));

    const { container, rerender } = render(
      <ConfidentialWatermark level="密" reader={READER} conversationId={101} />
    );
    expect(container.textContent).toContain("2026-07-30 15:30");
    expect(container.querySelector("[data-watermark-conversation='101']")).not.toBeNull();

    // Same conversation stays open — clock advances, stamp must not.
    vi.setSystemTime(new Date(2026, 6, 30, 16, 45, 0));
    rerender(
      <ConfidentialWatermark level="密" reader={READER} conversationId={101} />
    );
    expect(container.textContent).toContain("2026-07-30 15:30");
    expect(container.textContent).not.toContain("2026-07-30 16:45");

    // Switch to another conversation — stamp re-freezes to the new open minute.
    act(() => {
      rerender(
        <ConfidentialWatermark level="密" reader={READER} conversationId={202} />
      );
    });
    expect(container.textContent).toContain("2026-07-30 16:45");
    expect(container.textContent).not.toContain("2026-07-30 15:30");
    expect(container.querySelector("[data-watermark-conversation='202']")).not.toBeNull();
  });

  it("exposes an honest disclaimer constant for the controlled-conversation chrome", () => {
    expect(WATERMARK_DISCLAIMER).toBe("浮水印供外洩溯源，不阻止複製、截圖或列印。");
    expect(WATERMARK_DISCLAIMER).toContain("外洩溯源");
    expect(WATERMARK_DISCLAIMER).toContain("不阻止");
    expect(WATERMARK_DISCLAIMER).toContain("複製");
    expect(WATERMARK_DISCLAIMER).toContain("截圖");
    expect(WATERMARK_DISCLAIMER).toContain("列印");
    // Must not claim to block copying (the honest phrasing is「不阻止複製」).
    expect(WATERMARK_DISCLAIMER).not.toMatch(/(?<!不)阻止複製/);
    expect(WATERMARK_DISCLAIMER).not.toContain("已加密");
  });
});
