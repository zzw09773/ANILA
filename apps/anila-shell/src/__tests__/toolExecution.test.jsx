// Sprint 13 PR B3 — tool execution renderer tests.

import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  DiffOutput,
  FileTreeOutput,
  PlainOutput,
  TerminalOutput,
  ToolExecutionWidget,
  pickRenderer,
} from "../toolExecution.jsx";


// ---------------------------------------------------------------------
// pickRenderer
// ---------------------------------------------------------------------

describe("pickRenderer", () => {
  it("routes shell-class tools to TerminalOutput", () => {
    expect(pickRenderer("exec_bash")).toBe(TerminalOutput);
    expect(pickRenderer("exec_python")).toBe(TerminalOutput);
    expect(pickRenderer("shell")).toBe(TerminalOutput);
  });

  it("routes patch / edit tools to DiffOutput", () => {
    expect(pickRenderer("apply_patch")).toBe(DiffOutput);
    expect(pickRenderer("file_edit")).toBe(DiffOutput);
    expect(pickRenderer("edit")).toBe(DiffOutput);
  });

  it("routes file-listing tools to FileTreeOutput", () => {
    expect(pickRenderer("glob")).toBe(FileTreeOutput);
    expect(pickRenderer("ls")).toBe(FileTreeOutput);
    expect(pickRenderer("list_files")).toBe(FileTreeOutput);
  });

  it("falls back to PlainOutput for unknown tools", () => {
    expect(pickRenderer("read_doc")).toBe(PlainOutput);
    expect(pickRenderer("brand-new-tool")).toBe(PlainOutput);
  });
});


// ---------------------------------------------------------------------
// TerminalOutput
// ---------------------------------------------------------------------

describe("TerminalOutput", () => {
  it("renders the raw output text", () => {
    const { container } = render(<TerminalOutput output="hello world" />);
    expect(container.textContent).toBe("hello world");
  });

  it("shows a placeholder when output is empty", () => {
    render(<TerminalOutput output="" />);
    expect(screen.getByText("(empty)")).toBeTruthy();
  });

  it("paints an error border when status=error", () => {
    const { container } = render(
      <TerminalOutput output="boom" status="error" />,
    );
    expect(container.firstChild.style.border).toContain("var(--danger");
  });
});


// ---------------------------------------------------------------------
// DiffOutput
// ---------------------------------------------------------------------

describe("DiffOutput", () => {
  it("highlights added vs removed lines distinctly", () => {
    const diff =
      "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old line\n+new line\n unchanged";
    const { container } = render(<DiffOutput output={diff} />);
    // Six divs — one per line.
    const lines = container.querySelectorAll("div");
    expect(lines.length).toBe(6);
    // The +new line should carry the success / addition colour.
    const addedLine = Array.from(lines).find((d) =>
      d.textContent.includes("new line"),
    );
    expect(addedLine.style.color).toContain("var(--success");
    const removedLine = Array.from(lines).find((d) =>
      d.textContent.includes("old line") && !d.textContent.includes("---"),
    );
    expect(removedLine.style.color).toContain("var(--danger");
  });

  it("handles empty input gracefully", () => {
    render(<DiffOutput output="" />);
    expect(screen.getByText("(no diff returned)")).toBeTruthy();
  });
});


// ---------------------------------------------------------------------
// FileTreeOutput
// ---------------------------------------------------------------------

describe("FileTreeOutput", () => {
  it("parses a JSON array of paths", () => {
    render(
      <FileTreeOutput
        output={JSON.stringify([
          "src/a.py",
          "src/b.py",
          "tests/test_a.py",
        ])}
      />,
    );
    expect(screen.getByText("src/a.py")).toBeTruthy();
    expect(screen.getByText("src/b.py")).toBeTruthy();
    expect(screen.getByText("tests/test_a.py")).toBeTruthy();
  });

  it("falls back to newline-separated lines", () => {
    render(<FileTreeOutput output={"foo.txt\nbar.txt\nbaz.txt"} />);
    expect(screen.getByText("foo.txt")).toBeTruthy();
    expect(screen.getByText("bar.txt")).toBeTruthy();
    expect(screen.getByText("baz.txt")).toBeTruthy();
  });

  it("renders an empty-state message when the list is empty", () => {
    render(<FileTreeOutput output="" />);
    expect(screen.getByText("(no matches)")).toBeTruthy();
  });
});


// ---------------------------------------------------------------------
// ToolExecutionWidget
// ---------------------------------------------------------------------

describe("ToolExecutionWidget", () => {
  it("returns nothing without a call", () => {
    const { container } = render(<ToolExecutionWidget call={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders the tool name", () => {
    render(
      <ToolExecutionWidget
        call={{
          tool_call_id: "tc-1",
          tool_name: "exec_bash",
          status: "ok",
          output_preview: "ok",
        }}
      />,
    );
    expect(screen.getByText("exec_bash")).toBeTruthy();
  });

  it("uses the diff renderer for apply_patch", () => {
    const { container } = render(
      <ToolExecutionWidget
        call={{
          tool_call_id: "tc-2",
          tool_name: "apply_patch",
          status: "ok",
          output_preview: "+ added line\n- removed line\n unchanged",
        }}
      />,
    );
    // Check at least one line carries the success colour to confirm
    // DiffOutput rendered (PlainOutput would show flat text).
    const greenSpans = Array.from(container.querySelectorAll("div")).filter(
      (d) =>
        d.style.color &&
        d.style.color.includes("var(--success") &&
        d.textContent.includes("added line"),
    );
    expect(greenSpans.length).toBeGreaterThan(0);
  });

  it("uses the file tree renderer for glob", () => {
    render(
      <ToolExecutionWidget
        call={{
          tool_call_id: "tc-3",
          tool_name: "glob",
          status: "ok",
          output_preview: JSON.stringify(["a.py", "b.py"]),
        }}
      />,
    );
    expect(screen.getByText("a.py")).toBeTruthy();
    expect(screen.getByText("b.py")).toBeTruthy();
  });

  it("renders status copy 完成 for ok / 錯誤 for error", () => {
    const { rerender } = render(
      <ToolExecutionWidget
        call={{ tool_call_id: "x", tool_name: "shell", status: "ok", output_preview: "" }}
      />,
    );
    expect(screen.getByText("完成")).toBeTruthy();
    rerender(
      <ToolExecutionWidget
        call={{ tool_call_id: "x", tool_name: "shell", status: "error", output_preview: "boom" }}
      />,
    );
    expect(screen.getByText("錯誤")).toBeTruthy();
  });

  it("collapses by default for status=ok and stays open for running/error", () => {
    const { container, rerender } = render(
      <ToolExecutionWidget
        call={{ tool_call_id: "x", tool_name: "shell", status: "ok", output_preview: "" }}
      />,
    );
    const detailsOk = container.querySelector("details");
    expect(detailsOk.open).toBe(false);

    rerender(
      <ToolExecutionWidget
        call={{ tool_call_id: "x", tool_name: "shell", status: "running" }}
      />,
    );
    expect(container.querySelector("details").open).toBe(true);

    rerender(
      <ToolExecutionWidget
        call={{ tool_call_id: "x", tool_name: "shell", status: "error", output_preview: "x" }}
      />,
    );
    expect(container.querySelector("details").open).toBe(true);
  });
});
