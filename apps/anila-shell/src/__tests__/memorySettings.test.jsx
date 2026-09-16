import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";

import { ConfirmProvider } from "../confirm.jsx";
import { MemoryTab } from "../memory.jsx";

function renderTab(authRequest, extra = {}) {
  return render(
    <ConfirmProvider>
      <MemoryTab authRequest={authRequest} {...extra} />
    </ConfirmProvider>,
  );
}

function mockAuth({ pref = "", facts = [], chunks = [] } = {}) {
  return vi.fn(async (path, options = {}) => {
    const method = options.method || "GET";
    if (path === "/api/memory/preference" && method === "GET") {
      return { text: pref };
    }
    if (path === "/api/memory/preference" && method === "PUT") {
      const body = JSON.parse(options.body);
      return { text: String(body.text || "").trim() };
    }
    if (path === "/api/memory/facts" && method === "GET") {
      return { total: facts.length, facts };
    }
    if (path.startsWith("/api/memory/chunks") && method === "GET") {
      return {
        total: chunks.length,
        encrypted_total: 0,
        distinct_conversations: new Set(chunks.map((c) => c.conversation_id)).size,
        items: chunks,
      };
    }
    throw new Error(`unexpected ${method} ${path}`);
  });
}

describe("MemoryTab — 回覆偏好與來源對話", () => {
  it("saves the reply-style preference the user typed", async () => {
    const authRequest = mockAuth();
    renderTab(authRequest);
    await waitFor(() => expect(screen.getByTestId("memory-pref-input")).toBeTruthy());
    fireEvent.change(screen.getByTestId("memory-pref-input"), {
      target: { value: "請用繁體中文，先給結論" },
    });
    fireEvent.click(screen.getByTestId("memory-pref-save"));
    await waitFor(() => {
      const put = authRequest.mock.calls.find(
        ([path, opts]) => path === "/api/memory/preference" && opts?.method === "PUT",
      );
      expect(put).toBeTruthy();
      expect(JSON.parse(put[1].body)).toEqual({ text: "請用繁體中文，先給結論" });
    });
  });

  it("hides the reserved preference key from the extracted-fact list and links the source conversation", async () => {
    const opened = [];
    const authRequest = mockAuth({
      pref: "簡潔",
      facts: [
        { id: 1, key: "preference.reply_style", value: "簡潔", confidence: 1, source_conversation_id: null },
        { id: 2, key: "role", value: "工程師", confidence: 0.8, source_conversation_id: 44 },
      ],
    });
    renderTab(authRequest, { onOpenConversation: (id) => opened.push(id) });
    await waitFor(() => expect(screen.getByText("工程師")).toBeTruthy());
    expect(screen.queryByText("preference.reply_style")).toBeNull();
    expect(screen.getByTestId("memory-pref-input").value).toBe("簡潔");
    fireEvent.click(screen.getByTestId("memory-fact-source-2"));
    expect(opened).toEqual([44]);
  });
});
