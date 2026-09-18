import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import React from "react";

import { AuditWatermark } from "../trust.jsx";

afterEach(() => cleanup());

describe("AuditWatermark 窄視窗", () => {
  it("wraps and stays inside the message column", () => {
    render(
      <AuditWatermark
        traceId="trace-1788498859185"
        conversationId={129}
        latencyMs={1508}
        timestamp="2026-09-04T05:00:00Z"
        usage={{ total_tokens: 2024, prompt_tokens: 100, completion_tokens: 1924 }}
      />,
    );
    const btn = screen.getByTitle("複製追蹤資訊");
    expect(btn.className).toContain("anila-audit-watermark");
    const style = btn.getAttribute("style") || "";
    expect(style).toMatch(/flex-wrap:\s*wrap/i);
    expect(style).toMatch(/max-width:\s*100%/i);
  });
});
