// Sprint 13 PR B4 — span tree dev viewer tests.

import React from "react";
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import {
  SpanTreeViewer,
  countSpans,
  sumRootDuration,
} from "../spanTree.jsx";


// Force dev mode on so the component renders during tests.
beforeEach(() => {
  try {
    window.localStorage.setItem("anila_dev", "1");
  } catch {
    /* ignore */
  }
});


// ---------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------

describe("countSpans", () => {
  it("counts a flat list", () => {
    expect(countSpans([{ span_id: "a" }, { span_id: "b" }])).toBe(2);
  });

  it("counts nested children recursively", () => {
    expect(
      countSpans([
        {
          span_id: "root",
          children: [
            { span_id: "c1", children: [{ span_id: "leaf" }] },
            { span_id: "c2" },
          ],
        },
      ]),
    ).toBe(4);
  });

  it("returns 0 for non-arrays", () => {
    expect(countSpans(null)).toBe(0);
    expect(countSpans(undefined)).toBe(0);
  });
});


describe("sumRootDuration", () => {
  it("sums numeric duration_ms across roots", () => {
    expect(
      sumRootDuration([
        { span_id: "a", duration_ms: 100 },
        { span_id: "b", duration_ms: 200 },
      ]),
    ).toBe(300);
  });

  it("returns null when no root has a duration", () => {
    expect(
      sumRootDuration([{ span_id: "a", duration_ms: null }]),
    ).toBeNull();
  });

  it("returns null for empty input", () => {
    expect(sumRootDuration([])).toBeNull();
    expect(sumRootDuration(null)).toBeNull();
  });
});


// ---------------------------------------------------------------------
// SpanTreeViewer
// ---------------------------------------------------------------------

describe("SpanTreeViewer", () => {
  it("returns nothing when tree is empty", () => {
    const { container } = render(<SpanTreeViewer tree={[]} />);
    expect(container.firstChild).toBeNull();
  });

  // Note: the dev-mode "off" path is hard to exercise from the test
  // runner because Vitest sets ``import.meta.env.DEV=true``. The
  // ``devOnly={false}`` bypass test below covers the same surface
  // from the opposite direction (gate disabled → component renders).

  it("renders top-level span name + KindBadge in dev mode", () => {
    render(
      <SpanTreeViewer
        tree={[
          {
            span_id: "a",
            name: "agent_run",
            kind: "agent",
            status: "ok",
            duration_ms: 250,
            children: [],
          },
        ]}
      />,
    );
    expect(screen.getByText("agent_run")).toBeTruthy();
    // KindBadge text is uppercased via CSS but lives as the raw kind
    // string in the DOM text.
    expect(screen.getByText("agent")).toBeTruthy();
    // duration formatted to "250ms".
    expect(screen.getByText("250ms")).toBeTruthy();
  });

  it("renders summary count + total duration", () => {
    render(
      <SpanTreeViewer
        tree={[
          {
            span_id: "r",
            name: "root",
            kind: "run",
            status: "ok",
            duration_ms: 1234,
            children: [
              { span_id: "c", name: "leaf", kind: "tool", status: "ok" },
            ],
          },
        ]}
      />,
    );
    // 2 spans (root + child); root duration 1234ms → 1.23s
    expect(screen.getByText(/2 spans/)).toBeTruthy();
    expect(screen.getByText(/1\.23s total/)).toBeTruthy();
  });

  it("renders children when expanded", () => {
    render(
      <SpanTreeViewer
        tree={[
          {
            span_id: "r",
            name: "router",
            kind: "run",
            status: "ok",
            children: [
              {
                span_id: "c",
                name: "llm_call",
                kind: "llm",
                status: "ok",
              },
            ],
          },
        ]}
      />,
    );
    // depth<2 → child auto-open.
    expect(screen.getByText("llm_call")).toBeTruthy();
  });

  it("shows error text when a span carries an error", () => {
    render(
      <SpanTreeViewer
        tree={[
          {
            span_id: "r",
            name: "broken",
            kind: "tool",
            status: "error",
            error: "Connection refused",
          },
        ]}
      />,
    );
    expect(screen.getByText(/Connection refused/)).toBeTruthy();
  });

  it("renders span events with name and key=val attrs", () => {
    render(
      <SpanTreeViewer
        tree={[
          {
            span_id: "r",
            name: "agent_run",
            kind: "agent",
            status: "ok",
            events: [
              { name: "tool_start", attributes: { tool: "exec_python" } },
            ],
          },
        ]}
      />,
    );
    expect(screen.getByText(/tool_start/)).toBeTruthy();
    expect(screen.getByText(/tool=exec_python/)).toBeTruthy();
  });

  it("collapse arrow toggles when clicked", () => {
    render(
      <SpanTreeViewer
        tree={[
          {
            span_id: "p",
            name: "parent",
            kind: "run",
            status: "ok",
            children: [
              {
                span_id: "c",
                name: "child_to_hide",
                kind: "internal",
                status: "ok",
              },
            ],
          },
        ]}
      />,
    );
    expect(screen.getByText("child_to_hide")).toBeTruthy();
    // Click parent row to collapse.
    fireEvent.click(screen.getByText("parent"));
    expect(screen.queryByText("child_to_hide")).toBeNull();
  });

  it("respects devOnly=false to bypass the dev-mode check", () => {
    window.localStorage.removeItem("anila_dev");
    render(
      <SpanTreeViewer
        devOnly={false}
        tree={[{ span_id: "a", name: "shown", kind: "run", status: "ok" }]}
      />,
    );
    expect(screen.getByText("shown")).toBeTruthy();
  });
});
