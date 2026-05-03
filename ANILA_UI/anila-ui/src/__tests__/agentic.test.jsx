// Sprint 13 PR B2 — agentic UI component tests.

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import {
  FollowUpChips,
  InterruptCard,
  PausedBadge,
  TodoChecklist,
} from "../agentic.jsx";


// ---------------------------------------------------------------------
// PausedBadge
// ---------------------------------------------------------------------

describe("PausedBadge", () => {
  it("renders default ask_user copy", () => {
    render(<PausedBadge />);
    expect(screen.getByText(/等待您回答/)).toBeTruthy();
  });

  it("uses plan-specific copy for kind=plan", () => {
    render(<PausedBadge kind="plan" />);
    expect(screen.getByText(/等待您核准計畫/)).toBeTruthy();
  });

  it("uses tool-approval copy for kind=tool_approval", () => {
    render(<PausedBadge kind="tool_approval" />);
    expect(screen.getByText(/等待工具授權/)).toBeTruthy();
  });

  it("honours an explicit label", () => {
    render(<PausedBadge label="自訂文字" />);
    expect(screen.getByText("自訂文字")).toBeTruthy();
  });
});


// ---------------------------------------------------------------------
// InterruptCard — ask_user
// ---------------------------------------------------------------------

describe("InterruptCard ask_user", () => {
  const askPayload = {
    question: "Pick one",
    options: ["A", "B", "C"],
    multi_select: false,
    allow_other: false,
  };

  it("renders the question and options", () => {
    render(
      <InterruptCard
        kind="ask_user"
        payload={askPayload}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByText("Pick one")).toBeTruthy();
    expect(screen.getByRole("radio", { name: "A" })).toBeTruthy();
    expect(screen.getByRole("radio", { name: "B" })).toBeTruthy();
    expect(screen.getByRole("radio", { name: "C" })).toBeTruthy();
  });

  it("calls onSubmit with the radio selection", async () => {
    const onSubmit = vi.fn(async () => {});
    render(
      <InterruptCard
        kind="ask_user"
        payload={askPayload}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: "B" }));
    fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("B"));
  });

  it("returns an array when multi_select is true", async () => {
    const onSubmit = vi.fn(async () => {});
    render(
      <InterruptCard
        kind="ask_user"
        payload={{ ...askPayload, multi_select: true }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "A" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "C" }));
    fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(expect.arrayContaining(["A", "C"])),
    );
  });

  it("includes the 'other' free-text when allow_other and field used", async () => {
    const onSubmit = vi.fn(async () => {});
    render(
      <InterruptCard
        kind="ask_user"
        payload={{ ...askPayload, allow_other: true }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.change(screen.getByPlaceholderText(/或輸入其他回應/), {
      target: { value: "custom answer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("custom answer"));
  });

  it("does not submit when nothing is selected", () => {
    const onSubmit = vi.fn();
    render(
      <InterruptCard
        kind="ask_user"
        payload={askPayload}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("disables controls when disabled prop set", () => {
    render(
      <InterruptCard
        kind="ask_user"
        payload={askPayload}
        onSubmit={() => {}}
        disabled
      />,
    );
    expect(screen.getByRole("radio", { name: "A" }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "送出回答" }).disabled).toBe(true);
  });
});


// ---------------------------------------------------------------------
// InterruptCard — plan
// ---------------------------------------------------------------------

describe("InterruptCard plan", () => {
  it("renders plan text and accept/decline buttons", () => {
    render(
      <InterruptCard
        kind="plan"
        payload={{ plan: "Step 1\nStep 2" }}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByText(/Step 1/)).toBeTruthy();
    expect(screen.getByText("拒絕")).toBeTruthy();
    expect(screen.getByText("核准計畫")).toBeTruthy();
  });

  it("submits decision=accept on the primary button", async () => {
    const onSubmit = vi.fn(async () => {});
    render(
      <InterruptCard
        kind="plan"
        payload={{ plan: "do it" }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "核准計畫" }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({ decision: "accept" }),
    );
  });

  it("submits decision=decline on the secondary button", async () => {
    const onSubmit = vi.fn(async () => {});
    render(
      <InterruptCard
        kind="plan"
        payload={{ plan: "do it" }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "拒絕" }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({ decision: "decline" }),
    );
  });
});


// ---------------------------------------------------------------------
// InterruptCard — tool_approval
// ---------------------------------------------------------------------

describe("InterruptCard tool_approval", () => {
  it("renders the tool name + JSON-formatted input preview", () => {
    render(
      <InterruptCard
        kind="tool_approval"
        payload={{
          tool_name: "exec_python",
          tool_input: { code: "print(1)" },
        }}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByText("exec_python")).toBeTruthy();
    expect(screen.getByText(/print\(1\)/)).toBeTruthy();
  });

  it("submits approved=true / approved=false based on button", async () => {
    const onSubmit = vi.fn(async () => {});
    render(
      <InterruptCard
        kind="tool_approval"
        payload={{ tool_name: "x", tool_input: {} }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "授權執行" }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({ approved: true }),
    );
    onSubmit.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "拒絕" }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({ approved: false }),
    );
  });
});


// ---------------------------------------------------------------------
// InterruptCard — unknown kind
// ---------------------------------------------------------------------

describe("InterruptCard unknown kind", () => {
  it("falls back to a debug card without crashing", () => {
    render(
      <InterruptCard
        kind="future_kind"
        payload={{ x: 1 }}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByText(/未知中斷類型/)).toBeTruthy();
  });
});


// ---------------------------------------------------------------------
// TodoChecklist
// ---------------------------------------------------------------------

describe("TodoChecklist", () => {
  it("returns nothing when todos array is empty", () => {
    const { container } = render(<TodoChecklist todos={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders each todo's content", () => {
    render(
      <TodoChecklist
        todos={[
          { id: "1", content: "Read README", status: "completed" },
          { id: "2", content: "Write tests", status: "in_progress" },
          { id: "3", content: "Ship it", status: "pending" },
        ]}
      />,
    );
    expect(screen.getByText("Read README")).toBeTruthy();
    expect(screen.getByText("Write tests")).toBeTruthy();
    expect(screen.getByText("Ship it")).toBeTruthy();
  });

  it("strikes through completed todos", () => {
    render(
      <TodoChecklist
        todos={[{ id: "1", content: "Done thing", status: "completed" }]}
      />,
    );
    const row = screen.getByText("Done thing").parentElement;
    expect(row.style.textDecoration).toContain("line-through");
  });
});


// ---------------------------------------------------------------------
// FollowUpChips
// ---------------------------------------------------------------------

describe("FollowUpChips", () => {
  it("renders nothing when suggestions empty", () => {
    const { container } = render(<FollowUpChips suggestions={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("calls onPick(text) when a chip is clicked", () => {
    const onPick = vi.fn();
    render(
      <FollowUpChips
        suggestions={["Tell me more", "Summarise", "Why?"]}
        onPick={onPick}
      />,
    );
    fireEvent.click(screen.getByText("Summarise"));
    expect(onPick).toHaveBeenCalledWith("Summarise");
  });
});
