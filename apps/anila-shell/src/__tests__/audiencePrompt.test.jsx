import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import { Composer, MessageBubble } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { resetDraftAudienceMemory } from "../runtime/audienceDraft.js";

const LABELS = ["給客戶", "給同團隊", "給主管", "給承辦"];
const LETTER = "幫我擬一封給主管的說明函，說明週五改期";

function renderComposer(onSend = vi.fn()) {
  render(
    <ConfirmProvider>
      <Composer onSend={onSend} agents={[]} />
    </ConfirmProvider>,
  );
  return onSend;
}

function typeDraft(value) {
  const ta = screen.getByRole("textbox");
  fireEvent.change(ta, { target: { value } });
  return ta;
}

function audienceGroup() {
  return screen.getByRole("group", { name: "這是寫給誰的" });
}

beforeEach(() => {
  sessionStorage.clear();
  resetDraftAudienceMemory();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("送出前先問這是寫給誰的", () => {
  it("起草時先問四個選項，不預選、也不送出", () => {
    const onSend = renderComposer();
    typeDraft(LETTER);
    fireEvent.click(screen.getByRole("button", { name: "送出" }));

    expect(onSend).not.toHaveBeenCalled();
    const group = audienceGroup();
    const options = within(group).getAllByRole("button");
    expect(options.map((button) => button.textContent)).toEqual(LABELS);
    for (const button of options) {
      expect(button.getAttribute("aria-pressed")).toBe("false");
    }
    expect(screen.getByRole("button", { name: "給主管" }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.queryByRole("button", { name: "其他" })).toBeNull();
  });

  it("Enter 不會替同仁選第一個", () => {
    const onSend = renderComposer();
    const ta = typeDraft(LETTER);
    fireEvent.keyDown(ta, { key: "Enter" });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).not.toHaveBeenCalled();
    expect(within(audienceGroup()).getAllByRole("button")).toHaveLength(4);
    expect(screen.getByRole("button", { name: "給客戶" }).getAttribute("aria-pressed")).toBe("false");
  });

  it("點哪一個就只帶哪一個，文句裡的主管不會被拿來預選", () => {
    const onSend = renderComposer();
    typeDraft(LETTER);
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    fireEvent.click(screen.getByRole("button", { name: "給客戶" }));

    expect(onSend).toHaveBeenCalledTimes(1);
    const sent = onSend.mock.calls[0][0];
    expect(sent).toBe(`${LETTER}\n\n【這是寫給誰的】給客戶`);
    expect(sent).not.toMatch(/【這是寫給誰的】給主管/);
    expect(screen.queryByTestId("audience-prompt")).toBeNull();
  });

  it("同一份草稿再送一次不再問", () => {
    const onSend = renderComposer();
    typeDraft(LETTER);
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    fireEvent.click(screen.getByRole("button", { name: "給同團隊" }));
    typeDraft(LETTER);
    fireEvent.click(screen.getByRole("button", { name: "送出" }));

    expect(screen.queryByTestId("audience-prompt")).toBeNull();
    expect(onSend).toHaveBeenCalledTimes(2);
    expect(onSend.mock.calls[1][0]).toMatch(/【這是寫給誰的】給同團隊$/);
  });

  it("送出失敗後，同一份草稿還留著上次的選擇", () => {
    const onSend = vi.fn(() => false);
    renderComposer(onSend);
    const ta = typeDraft(LETTER);
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    fireEvent.click(screen.getByRole("button", { name: "給承辦" }));

    expect(ta.value).toBe(LETTER);
    expect(onSend).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(screen.queryByTestId("audience-prompt")).toBeNull();
    expect(onSend).toHaveBeenCalledTimes(2);
    expect(onSend.mock.calls[1][0]).toMatch(/【這是寫給誰的】給承辦$/);
  });

  it("先不要送就留在輸入框", () => {
    const onSend = renderComposer();
    typeDraft(LETTER);
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    fireEvent.click(screen.getByTestId("audience-dismiss"));

    expect(onSend).not.toHaveBeenCalled();
    expect(screen.queryByTestId("audience-prompt")).toBeNull();
    expect(screen.getByRole("textbox").value).toBe(LETTER);
  });

  it("不是起草的問題直接送出", () => {
    const onSend = renderComposer();
    const question = "這份規章第 3 條在說什麼";
    typeDraft(question);
    fireEvent.click(screen.getByRole("button", { name: "送出" }));

    expect(screen.queryByTestId("audience-prompt")).toBeNull();
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0]).toBe(question);
  });
});

describe("編輯後重送同一份草稿", () => {
  function renderUser(text, onEditUser = vi.fn()) {
    render(
      <ConfirmProvider>
        <MessageBubble
          msg={{ id: "u1", role: "user", text, attachments: [] }}
          agents={[]}
          conversationId={1}
          onEditUser={onEditUser}
        />
      </ConfirmProvider>,
    );
    return onEditUser;
  }

  it("還沒選過的起草，編輯送出時一樣要問，而且不預選", () => {
    const onEditUser = renderUser(LETTER);
    fireEvent.click(screen.getByTitle("編輯"));
    fireEvent.click(screen.getByRole("button", { name: "送出" }));

    expect(onEditUser).not.toHaveBeenCalled();
    const options = within(audienceGroup()).getAllByRole("button");
    expect(options.map((button) => button.textContent)).toEqual(LABELS);
    for (const button of options) {
      expect(button.getAttribute("aria-pressed")).toBe("false");
    }

    fireEvent.click(screen.getByRole("button", { name: "給承辦" }));
    expect(onEditUser).toHaveBeenCalledTimes(1);
    expect(onEditUser.mock.calls[0][1]).toBe(`${LETTER}\n\n【這是寫給誰的】給承辦`);
  });

  it("氣泡顯示已經選過的對象，再送出同一份不再問", () => {
    const marked = `${LETTER}\n\n【這是寫給誰的】給主管`;
    const onEditUser = renderUser(marked);
    expect(screen.getByTestId("audience-chosen").textContent).toBe("這是寫給誰的：給主管");
    expect(screen.queryByText(/【這是寫給誰的】/)).toBeNull();

    fireEvent.click(screen.getByTitle("編輯"));
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(screen.queryByTestId("audience-prompt")).toBeNull();
    expect(onEditUser).toHaveBeenCalledTimes(1);
    expect(onEditUser.mock.calls[0][1]).toBe(marked);
  });
});
