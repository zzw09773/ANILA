// Edit-resend invariants (owner 2026-08-01):
// 1. Identical text is a legitimate re-send — short-circuit on equality is forbidden
//    at BOTH chat.jsx saveEdit and app.jsx handleEditUser.
// 2. Empty draft never sends — UI disables submit, saveEdit cancels, handler aborts.
//
// Shape mirrors uxCopy.test.jsx: pure helper for the decision + source guards so
// a call-site regression (reintroducing `trimmed === userMsg.text`) cannot hide
// behind a green helper suite.

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { resolveEditResend } from "../runtime/editResend.js";
import { MessageBubble } from "../chat.jsx";

afterEach(cleanup);

const src = (rel) => readFileSync(resolve(process.cwd(), "src", rel), "utf8");

/** Slice a function body by name so guards stay local to the send path. */
function functionBody(source, name) {
  const start = source.indexOf(`function ${name}`);
  expect(start, `${name} not found`).toBeGreaterThanOrEqual(0);
  let depth = 0;
  let began = false;
  for (let i = start; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") {
      depth += 1;
      began = true;
    } else if (ch === "}") {
      depth -= 1;
      if (began && depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

describe("resolveEditResend (pure gate)", () => {
  it("aborts on empty / whitespace-only drafts", () => {
    expect(resolveEditResend("")).toEqual({ ok: false });
    expect(resolveEditResend("   \n\t  ")).toEqual({ ok: false });
    expect(resolveEditResend(null)).toEqual({ ok: false });
    expect(resolveEditResend(undefined)).toEqual({ ok: false });
  });

  it("allows send when text is unchanged (identical re-send)", () => {
    const current = "原問句";
    const decision = resolveEditResend(current);
    expect(decision).toEqual({ ok: true, text: current });
    // Gate must not take / compare against the prior message text.
    expect(decision.ok).toBe(true);
  });

  it("trims but still sends non-empty drafts", () => {
    expect(resolveEditResend("  hello  ")).toEqual({ ok: true, text: "hello" });
  });
});

describe("MessageBubble edit re-send (UI half)", () => {
  function renderEditableUser(text, onEditUser) {
    return render(
      React.createElement(MessageBubble, {
        msg: {
          id: "u1",
          dbId: 11,
          role: "user",
          text,
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [11],
        },
        agents: [],
        onEditUser,
      }),
    );
  }

  it("keeps 送出 enabled and fires onEditUser when text is unchanged", () => {
    const onEditUser = vi.fn();
    renderEditableUser("原問句", onEditUser);

    fireEvent.click(document.querySelector("[data-edit-btn]"));
    const submit = screen.getByRole("button", { name: "送出" });
    expect(submit.disabled).toBe(false);

    fireEvent.click(submit);
    expect(onEditUser).toHaveBeenCalledTimes(1);
    expect(onEditUser.mock.calls[0][1]).toBe("原問句");
  });

  it("empty draft: 送出 disabled, Ctrl+Enter cancels without calling onEditUser", () => {
    // Ctrl+Enter bypasses the disabled attribute and hits saveEdit directly —
    // that is the path that fails if the empty-guard inside saveEdit is removed.
    const onEditUser = vi.fn();
    renderEditableUser("原問句", onEditUser);

    fireEvent.click(document.querySelector("[data-edit-btn]"));
    const textarea = document.querySelector("textarea");
    expect(textarea).toBeTruthy();
    fireEvent.change(textarea, { target: { value: "" } });

    const submit = screen.getByRole("button", { name: "送出" });
    expect(submit.disabled).toBe(true);

    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(onEditUser).not.toHaveBeenCalled();
    // cancelEdit leaves edit mode — textarea gone, original text visible again.
    expect(document.querySelector("textarea")).toBeNull();
    expect(screen.getByText("原問句")).toBeTruthy();
  });
});

describe("原始碼護欄: edit-resend call sites share resolveEditResend", () => {
  const appSrc = src("app.jsx");
  const chatSrc = src("chat.jsx");
  const handleBody = functionBody(appSrc, "handleEditUser");

  it("app.jsx handleEditUser gates through resolveEditResend only", () => {
    expect(handleBody).toContain("resolveEditResend(nextText)");
    // Mutation A: `if (!trimmed || trimmed === userMsg.text) return` must turn red.
    expect(handleBody).not.toMatch(/===\s*userMsg\.text/);
    expect(handleBody).not.toMatch(/userMsg\.text\s*===/);
  });

  it("chat.jsx saveEdit / submit disable gate through resolveEditResend", () => {
    expect(chatSrc).toContain("resolveEditResend(draft)");
    expect(chatSrc).toContain("canSubmitEdit");
    // Identical-text short-circuit in the bubble half must turn red.
    expect(chatSrc).not.toMatch(/draft\.trim\(\)\s*===\s*msg\.text/);
    expect(chatSrc).not.toMatch(/next\s*===\s*msg\.text/);
    expect(chatSrc).not.toMatch(/decision\.text\s*===\s*msg\.text/);
  });

  it("empty-abort remains the only early return before auth in handleEditUser", () => {
    // If the empty-guard is deleted and resolveEditResend is bypassed, this fails.
    expect(handleBody).toMatch(/if\s*\(\s*!decision\.ok\s*\)\s*return/);
  });
});
