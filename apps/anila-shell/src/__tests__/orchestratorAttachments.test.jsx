// 新對話上傳必須先建 conv，送出前必須 bind；bind 失敗不得開串流。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup, fireEvent, waitFor, act } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForIdle,
  composerBox,
  createFakeBackend,
  screen,
} from "./helpers/orchestrator.jsx";
import { errorFrame, headerValue } from "./helpers/fakeBackend.js";

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

  it("上傳中不送出；完成後訊息帶 referenceId，bind 用同一個 id", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueAnswer("已讀到附件");
    let releaseUpload;
    backend.route("POST", "/api/attachments", () => new Promise((resolve) => {
      releaseUpload = resolve;
    }), { once: true });
    await mountOrchestrator({ backend });

    const file = new File(["內容"], "note.md", { type: "text/markdown" });
    await pickComposerFile(file);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "附件上傳中，完成後才能送出" })).toBeDisabled();
    });
    fireEvent.change(composerBox(), { target: { value: "請摘要這個檔案" } });
    fireEvent.keyDown(composerBox(), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "附件上傳中，完成後才能送出" }));
    expect(backend.chatPayloads).toHaveLength(0);
    expect(backend.requestsFor("/api/attachments/bind", "POST")).toHaveLength(0);
    expect(composerBox().value).toBe("請摘要這個檔案");

    await act(async () => { releaseUpload(undefined); });
    await waitForReadyChip();
    await sendText("請摘要這個檔案");
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });

    const bind = backend.requestsFor("/api/attachments/bind", "POST")[0];
    expect(bind.body.reference_ids).toEqual([expect.stringMatching(/^att-ref-/)]);
    const ref = bind.body.reference_ids[0];
    const chat = backend.requests.find(
      (r) => r.method === "POST" && r.path === "/v1/chat/completions" && r.body?.stream !== false,
    );
    expect(headerValue(chat, "X-ANILA-Attachment-Refs")).toBe(ref);
    const content = chat.body.messages.at(-1).content;
    expect(content).toContain("note.md");
    expect(content).toContain("請摘要這個檔案");
  });

  it("上傳失敗時不送出該檔，原文留在輸入框", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueAnswer("只有文字");
    let failUpload;
    backend.route("POST", "/api/attachments", (_req, { errorResponse }) => new Promise((resolve) => {
      failUpload = () => resolve(errorResponse(400, "note.md 上傳失敗"));
    }), { once: true });
    await mountOrchestrator({ backend });

    fireEvent.change(composerBox(), { target: { value: "請摘要" } });
    await pickComposerFile(new File(["內容"], "note.md", { type: "text/markdown" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "附件上傳中，完成後才能送出" })).toBeDisabled();
    });
    fireEvent.keyDown(composerBox(), { key: "Enter" });
    expect(backend.chatPayloads).toHaveLength(0);

    await act(async () => { failUpload(); });
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("note.md 上傳失敗");
    });
    expect(document.querySelector(".composer-att-chip")).toBeNull();
    expect(composerBox().value).toBe("請摘要");

    await sendText("請摘要");
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
    expect(backend.requestsFor("/api/attachments/bind", "POST")).toHaveLength(0);
    const chat = backend.requests.find(
      (r) => r.method === "POST" && r.path === "/v1/chat/completions" && r.body?.stream !== false,
    );
    expect(headerValue(chat, "X-ANILA-Attachment-Refs")).toBeUndefined();
    expect(chat.body.messages.at(-1).content).toBe("請摘要");
  });

  it("bind 失敗時原文與附件回到輸入框，可再送出同一個 referenceId", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueAnswer("不該出現").enqueueAnswer("第二次才送到");
    await mountOrchestrator({ backend });

    await pickComposerFile(new File(["內容"], "note.md", { type: "text/markdown" }));
    await waitForReadyChip();
    backend.route("POST", "/api/attachments/bind", (_req, { errorResponse }) =>
      errorResponse(500, "網路中斷"),
    { once: true });

    await sendText("請摘要這個檔案");
    await waitFor(() => {
      expect(screen.getAllByText("網路中斷").length).toBeGreaterThan(0);
    });
    expect(backend.chatPayloads).toHaveLength(0);
    expect(composerBox().value).toBe("請摘要這個檔案");
    const chip = document.querySelector(".composer-att-chip");
    expect(chip).toBeTruthy();
    expect(chip.textContent).toContain("note.md");

    await sendText("請摘要這個檔案");
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
    const binds = backend.requestsFor("/api/attachments/bind", "POST");
    // 失敗那次一次；再送出時送出前綁一次，落庫後再釘到訊息上一次。
    expect(binds.length).toBeGreaterThanOrEqual(2);
    const ids = binds.map((bind) => bind.body.reference_ids);
    expect(ids[0]).toEqual([expect.stringMatching(/^att-ref-/)]);
    expect(ids.every((row) => JSON.stringify(row) === JSON.stringify(ids[0]))).toBe(true);
  });

  it("bind 還在等時打了新字，失敗後新字留著，還原才換回原文與附件", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    let releaseBind;
    backend.route("POST", "/api/attachments/bind", () => new Promise((resolve) => {
      releaseBind = resolve;
    }), { once: true });
    await mountOrchestrator({ backend });

    await pickComposerFile(new File(["內容"], "old.md", { type: "text/markdown" }));
    await waitForReadyChip();
    await sendText("原始內容");
    await waitFor(() => {
      expect(backend.requestsFor("/api/attachments/bind", "POST").length).toBe(1);
    });
    expect(composerBox().value).toBe("");

    fireEvent.change(composerBox(), { target: { value: "新草稿" } });
    await act(async () => {
      releaseBind({
        ok: false,
        status: 500,
        statusText: "HTTP 500",
        headers: { get: () => "application/json" },
        json: async () => ({ detail: "網路中斷" }),
        text: async () => JSON.stringify({ detail: "網路中斷" }),
      });
    });
    await waitFor(() => {
      expect(screen.getAllByText("網路中斷").length).toBeGreaterThan(0);
    });
    expect(composerBox().value).toBe("新草稿");
    expect(document.querySelector(".composer-att-chip")).toBeNull();
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("原始內容");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("old.md");

    fireEvent.click(screen.getByRole("button", { name: "還原未送出的訊息" }));
    expect(composerBox().value).toBe("原始內容");
    expect(document.querySelector(".composer-att-chip").textContent).toContain("old.md");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("新草稿");
    expect(backend.chatPayloads).toHaveLength(0);
  });

  it("串流中途失敗時原文留在氣泡，新草稿留在輸入框，兩份不合併", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueManualStream();
    await mountOrchestrator({ backend });
    await sendText("原始問題");
    await waitFor(() => {
      expect(screen.getByLabelText("停止產生")).toBeTruthy();
    });
    expect(screen.getAllByText("原始問題").length).toBeGreaterThan(0);

    fireEvent.change(composerBox(), { target: { value: "下一則草稿" } });
    await act(async () => {
      backend.stream.push(errorFrame({ message: "模型端點暫時無法使用" }));
      backend.stream.close();
    });
    await waitForIdle();

    expect(composerBox().value).toBe("下一則草稿");
    expect(screen.getAllByText("原始問題").length).toBeGreaterThan(0);
    expect(composerBox().value).not.toContain("原始問題");
    expect(screen.getByTestId("message-stream-error").textContent).toMatch(/模型端點暫時無法使用/);
    expect(screen.queryByRole("button", { name: "還原未送出的訊息" })).toBeNull();
  });

  it("停用模型把原文放回時，不蓋掉串流期間打的新草稿", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueManualStream();
    await mountOrchestrator({ backend });
    await sendText("原始問題");
    await waitFor(() => {
      expect(screen.getByLabelText("停止產生")).toBeTruthy();
    });
    fireEvent.change(composerBox(), { target: { value: "下一則草稿" } });
    await act(async () => {
      backend.stream.push(errorFrame({
        code: "model_unavailable",
        reason: "inactive",
        display_name: "glm-5.3-flash",
        name: "glm-5.3-flash",
        message: "ignored",
      }));
      backend.stream.close();
    });
    await waitForIdle();

    expect(composerBox().value).toBe("下一則草稿");
    expect(screen.getAllByText("原始問題").length).toBeGreaterThan(0);
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("原始問題");
    fireEvent.click(screen.getByRole("button", { name: "還原未送出的訊息" }));
    expect(composerBox().value).toBe("原始問題");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("下一則草稿");
  });
});
