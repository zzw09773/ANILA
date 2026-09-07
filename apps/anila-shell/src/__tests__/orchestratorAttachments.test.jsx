// 新對話上傳必須先建 conv，送出前必須 bind；bind 失敗不得開串流。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup, fireEvent, waitFor, act } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForIdle,
  createFakeBackend,
  screen,
} from "./helpers/orchestrator.jsx";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function pickComposerFile(file) {
  const input = document.querySelector('input[type="file"]');
  expect(input).toBeTruthy();
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } });
  });
}

async function waitForReadyChip() {
  await waitFor(() => {
    const chip = document.querySelector('.composer-att-chip[data-extract-status="ok"]');
    expect(chip).toBeTruthy();
  });
}

describe("orchestrator — 附件綁對話", () => {
  it("新對話上傳會先建立 conversation，再用回傳的 conv id 上傳", async () => {
    const backend = createFakeBackend();
    const { container } = await mountOrchestrator({ backend });
    expect(container).toBeTruthy();

    const file = new File(["內容"], "note.md", { type: "text/markdown" });
    await pickComposerFile(file);

    await waitFor(() => {
      const uploads = backend.requests.filter(
        (r) => r.method === "POST" && r.path === "/api/attachments",
      );
      expect(uploads).toHaveLength(1);
    });

    const convCreates = backend.requests.filter(
      (r) => r.method === "POST" && r.path === "/api/conversations",
    );
    expect(convCreates).toHaveLength(1);
    const convIdx = backend.requests.findIndex(
      (r) => r.method === "POST" && r.path === "/api/conversations",
    );
    const upIdx = backend.requests.findIndex(
      (r) => r.method === "POST" && r.path === "/api/attachments",
    );
    expect(upIdx).toBeGreaterThan(convIdx);

    const upload = backend.requests[upIdx];
    expect(upload.init.body).toBeInstanceOf(FormData);
    const convId = backend.conversationIds()[0];
    expect(upload.init.body.get("conversation_id")).toBe(String(convId));
  });

  it("送出前會 bind 附件；bind 失敗則不開串流", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("不該出現");
    await mountOrchestrator({ backend });

    const file = new File(["內容"], "note.md", { type: "text/markdown" });
    await pickComposerFile(file);
    await waitForReadyChip();

    backend.route("POST", "/api/attachments/bind", (_req, { errorResponse }) =>
      errorResponse(409, "此附件已綁定其他對話"),
    );

    await sendText("請摘要這個檔案");
    await waitFor(() => {
      expect(screen.getAllByText("此附件已綁定其他對話").length).toBeGreaterThan(0);
    });
    expect(backend.chatPayloads).toHaveLength(0);
    await waitForIdle();
  });

  it("送出成功時 bind 發生在串流之前", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("已讀到附件");
    await mountOrchestrator({ backend });

    const file = new File(["內容"], "note.md", { type: "text/markdown" });
    await pickComposerFile(file);
    await waitForReadyChip();

    await sendText("請摘要這個檔案");
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });

    const bindIdx = backend.requests.findIndex(
      (r) => r.method === "POST" && r.path === "/api/attachments/bind",
    );
    const chatIdx = backend.requests.findIndex(
      (r) =>
        r.method === "POST"
        && r.path === "/v1/chat/completions"
        && r.body?.stream !== false,
    );
    expect(bindIdx).toBeGreaterThanOrEqual(0);
    expect(chatIdx).toBeGreaterThan(bindIdx);
    const bindBody = backend.requests[bindIdx].body;
    expect(bindBody.conversation_id).toBe(backend.conversationIds()[0]);
    expect(bindBody.reference_ids.length).toBe(1);
  });
});
