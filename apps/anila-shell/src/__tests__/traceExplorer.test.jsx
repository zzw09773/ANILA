// Slice 4d — spansToTree (pure) + TraceExplorer (integration-ish) tests.

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { spansToTree, TraceExplorer } from "../spanTree.jsx";

// A flat, backend-shaped span (Trace Span Schema, §09 API contracts).
function span(overrides) {
  return {
    span_id: "s",
    parent_span_id: null,
    trace_id: "tr-1",
    span_type: "agent_run",
    name: "span",
    status: "ok",
    started_at: "2026-07-01T00:00:00Z",
    ended_at: "2026-07-01T00:00:01Z",
    attributes: {},
    producer: "router",
    ...overrides,
  };
}

// ---------------------------------------------------------------------
// spansToTree
// ---------------------------------------------------------------------

describe("spansToTree", () => {
  it("returns [] for non-arrays / empty", () => {
    expect(spansToTree(null)).toEqual([]);
    expect(spansToTree(undefined)).toEqual([]);
    expect(spansToTree([])).toEqual([]);
  });

  it("nests children under their parent_span_id", () => {
    const tree = spansToTree([
      span({ span_id: "root", parent_span_id: null }),
      span({ span_id: "child", parent_span_id: "root" }),
      span({ span_id: "grand", parent_span_id: "child" }),
    ]);
    expect(tree).toHaveLength(1);
    expect(tree[0].span_id).toBe("root");
    expect(tree[0].children).toHaveLength(1);
    expect(tree[0].children[0].span_id).toBe("child");
    expect(tree[0].children[0].children[0].span_id).toBe("grand");
  });

  it("promotes orphan parents (missing parent) to roots — never drops them", () => {
    const tree = spansToTree([
      span({ span_id: "a", parent_span_id: "ghost" }),
      span({ span_id: "b", parent_span_id: null }),
    ]);
    const rootIds = tree.map((n) => n.span_id).sort();
    expect(rootIds).toEqual(["a", "b"]);
  });

  it("treats a self-referential parent as a root (no infinite loop)", () => {
    const tree = spansToTree([span({ span_id: "self", parent_span_id: "self" })]);
    expect(tree).toHaveLength(1);
    expect(tree[0].span_id).toBe("self");
    expect(tree[0].children).toEqual([]);
  });

  it("orders roots and children ascending by started_at (stable)", () => {
    const tree = spansToTree([
      span({ span_id: "late", started_at: "2026-07-01T00:00:03Z" }),
      span({ span_id: "early", started_at: "2026-07-01T00:00:01Z" }),
      span({ span_id: "mid", started_at: "2026-07-01T00:00:02Z" }),
    ]);
    expect(tree.map((n) => n.span_id)).toEqual(["early", "mid", "late"]);
  });

  it("sorts children by started_at within a parent", () => {
    const tree = spansToTree([
      span({ span_id: "p", parent_span_id: null, started_at: "2026-07-01T00:00:00Z" }),
      span({ span_id: "c2", parent_span_id: "p", started_at: "2026-07-01T00:00:05Z" }),
      span({ span_id: "c1", parent_span_id: "p", started_at: "2026-07-01T00:00:02Z" }),
    ]);
    expect(tree[0].children.map((n) => n.span_id)).toEqual(["c1", "c2"]);
  });

  it("keeps undated spans in input order and sorts them last", () => {
    const tree = spansToTree([
      span({ span_id: "nod2", started_at: null }),
      span({ span_id: "dated", started_at: "2026-07-01T00:00:01Z" }),
      span({ span_id: "nod1", started_at: null }),
    ]);
    expect(tree.map((n) => n.span_id)).toEqual(["dated", "nod2", "nod1"]);
  });

  it("maps span_type→kind, computes duration_ms, carries status/attributes", () => {
    const [node] = spansToTree([
      span({
        span_id: "x",
        span_type: "llm_call",
        status: "ok",
        started_at: "2026-07-01T00:00:00Z",
        ended_at: "2026-07-01T00:00:02Z",
        attributes: { model: "gpt-oss" },
      }),
    ]);
    expect(node.kind).toBe("llm_call");
    expect(node.status).toBe("ok");
    expect(node.duration_ms).toBe(2000);
    expect(node.attributes).toEqual({ model: "gpt-oss" });
  });

  it("surfaces an error string for failed spans", () => {
    const [node] = spansToTree([
      span({ span_id: "e", status: "error", attributes: { error: "boom" } }),
    ]);
    expect(node.status).toBe("error");
    expect(node.error).toBe("boom");
  });
});

// ---------------------------------------------------------------------
// TraceExplorer
// ---------------------------------------------------------------------

describe("TraceExplorer", () => {
  it("renders nothing without a traceId (no affordance)", () => {
    const { container } = render(<TraceExplorer traceId={null} />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("檢視軌跡")).toBeNull();
  });

  it("shows the 檢視軌跡 affordance when a traceId is present", () => {
    render(<TraceExplorer traceId="tr-1" fetchTrace={vi.fn()} />);
    expect(screen.getByText(/檢視軌跡/)).toBeTruthy();
  });

  it("click → mocked fetch → SpanTreeViewer renders the converted tree", async () => {
    const fetchTrace = vi.fn().mockResolvedValue({
      trace_id: "tr-1",
      task_id: 7,
      spans: [
        span({ span_id: "root", name: "router", parent_span_id: null }),
        span({ span_id: "child", name: "risk-agent", parent_span_id: "root" }),
      ],
    });
    render(<TraceExplorer traceId="tr-1" fetchTrace={fetchTrace} />);

    fireEvent.click(screen.getByText(/檢視軌跡/));

    // SpanTreeViewer renders both converted nodes (details content is in DOM).
    expect(await screen.findByText("router")).toBeTruthy();
    expect(screen.getByText("risk-agent")).toBeTruthy();
    expect(fetchTrace).toHaveBeenCalledWith("tr-1");
  });

  it("fetch failure (null) → inline 尚無軌跡資料 notice, no crash", async () => {
    const fetchTrace = vi.fn().mockResolvedValue(null);
    render(<TraceExplorer traceId="tr-1" fetchTrace={fetchTrace} />);

    fireEvent.click(screen.getByText(/檢視軌跡/));
    expect(await screen.findByText("尚無軌跡資料")).toBeTruthy();
  });

  it("empty spans → 尚無軌跡資料 notice", async () => {
    const fetchTrace = vi.fn().mockResolvedValue({ trace_id: "tr-1", spans: [] });
    render(<TraceExplorer traceId="tr-1" fetchTrace={fetchTrace} />);

    fireEvent.click(screen.getByText(/檢視軌跡/));
    expect(await screen.findByText("尚無軌跡資料")).toBeTruthy();
  });

  it("renders live spans immediately, then replaces them on 檢視軌跡 click", async () => {
    const fetchTrace = vi.fn().mockResolvedValue({
      trace_id: "tr-1",
      spans: [span({ span_id: "p", name: "persisted-span" })],
    });
    render(
      <TraceExplorer
        traceId="tr-1"
        liveSpans={[span({ span_id: "l", name: "live-span" })]}
        fetchTrace={fetchTrace}
      />,
    );

    // Live span shown before any click.
    expect(screen.getByText("live-span")).toBeTruthy();

    fireEvent.click(screen.getByText(/檢視軌跡/));

    // Persisted replaces live (simple swap, no merge).
    expect(await screen.findByText("persisted-span")).toBeTruthy();
    expect(screen.queryByText("live-span")).toBeNull();
  });
});
