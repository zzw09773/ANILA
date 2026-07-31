/**
 * Fixed-epoch: API timestamps with an explicit UTC offset render as Taipei wall clock.
 */
import { describe, expect, it } from "vitest";

describe("API timestamp with UTC offset → Asia/Taipei wall clock", () => {
  const apiUtc = "2026-07-30T08:00:00.000000+00:00";

  it("parses offset-bearing API string as UTC, not local", () => {
    const d = new Date(apiUtc);
    expect(Number.isNaN(d.getTime())).toBe(false);
    const rendered = d.toLocaleString("zh-TW", {
      timeZone: "Asia/Taipei",
      hour12: false,
    });
    expect(rendered).toMatch(/16:00:00/);
  });

  it("Z suffix is equivalent to +00:00", () => {
    const rendered = new Date("2026-07-30T08:00:00.000000Z").toLocaleString(
      "zh-TW",
      { timeZone: "Asia/Taipei", hour12: false },
    );
    expect(rendered).toMatch(/16:00:00/);
  });
});
