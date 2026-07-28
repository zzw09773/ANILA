/**
 * Fixed-epoch: API timestamps with an explicit UTC offset render as Taipei wall clock.
 *
 * Regression for naive ISO strings (no offset) being parsed as local time by
 * ECMAScript — which made UTC 11:19 look like Taipei 11:19 ("8 hours ago").
 */
import { describe, expect, it } from "vitest";
import { taipeiIso, taipeiStamp } from "../runtime/exportHeader.js";

describe("API timestamp with UTC offset → Asia/Taipei wall clock", () => {
  // Same instant as the measured ingestion_collections.created_at bug.
  const apiUtc = "2026-07-27T11:19:42.679819+00:00";

  it("parses offset-bearing API string as UTC, not local", () => {
    const ms = Date.parse(apiUtc);
    expect(Number.isNaN(ms)).toBe(false);
    // Taipei = UTC+8 → 19:19:42 on the same calendar day.
    expect(taipeiIso(apiUtc)).toBe("2026-07-27T19:19:42+08:00");
    expect(taipeiStamp(apiUtc)).toContain("2026-07-27 19:19:42");
  });

  it("Z suffix is equivalent to +00:00", () => {
    expect(taipeiIso("2026-07-27T11:19:42.679819Z")).toBe(
      "2026-07-27T19:19:42+08:00",
    );
  });
});
