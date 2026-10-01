// 附件還在上傳時不能送出。送出失敗時原文與附件要回得來；
// 框裡已經有新內容就不要蓋掉，用「還原未送出的訊息」整份換。

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, act } from "@testing-library/react";

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

const AGENTS = [{ id: "anila-router", name: "ANILA", short: "auto" }];
const BLOCKED = "附件上傳中，完成後才能送出";

function renderComposer(props = {}) {
  const onSend = props.onSend || vi.fn();
  const utils = render(
    <ConfirmProvider>
      <Composer onSend={onSend} agents={AGENTS} {...props} />
    </ConfirmProvider>,
  );
  return {
    ...utils,
    onSend,
    rerender(next) {
      utils.rerender(
        <ConfirmProvider>
          <Composer onSend={onSend} agents={AGENTS} {...props} {...next} />
        </ConfirmProvider>,
      );
    },
  };
}

function fileInput(container) {
  return container.querySelector('input[type="file"]');
}

async function pickFile(container, file) {
  await act(async () => {
    fireEvent.change(fileInput(container), { target: { files: [file] } });
  });
}

function chip(name) {
  return [...document.querySelectorAll(".composer-att-chip")].find((node) =>
    node.textContent.includes(name),
  ) || null;
}

function typeText(value) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value } });
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe("上傳中不能送出", () => {
  it("Enter 與送出鈕在上傳完成前不送，完成後才帶 referenceId", async () => {
    let resolveUpload;
    const onUpload = vi.fn(() => new Promise((resolve) => {
      resolveUpload = resolve;
    }));
    const onSend = vi.fn();
    const { container } = renderComposer({ onSend, onUpload });
    const ta = screen.getByRole("textbox");
    typeText("請整理");
    await pickFile(container, new File(["hello"], "note.txt", { type: "text/plain" }));

    const blocked = screen.getByRole("button", { name: BLOCKED });
    expect(blocked).toBeDisabled();
    expect(blocked).toHaveAttribute("title", BLOCKED);
    expect(chip("note.txt").textContent).toContain("上傳中");

    fireEvent.click(blocked);
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(ta.value).toBe("請整理");
    expect(chip("note.txt")).toBeTruthy();

    await act(async () => {
      resolveUpload({
        filename: "note.txt",
        reference_id: "ref-note",
        content_type: "text/plain",
        size_bytes: 5,
        extract_status: "ok",
      });
    });

    expect(chip("note.txt").textContent).not.toContain("上傳中");
    const send = screen.getByRole("button", { name: "送出" });
    expect(send).not.toBeDisabled();
    fireEvent.click(send);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0]).toBe("請整理");
    expect(onSend.mock.calls[0][1]).toEqual([
      expect.objectContaining({
        name: "note.txt",
        referenceId: "ref-note",
        uploading: false,
      }),
    ]);
    expect(ta.value).toBe("");
    expect(chip("note.txt")).toBeNull();
  });

  it("上傳失敗會留下錯誤與原文，失敗的檔不會被送出", async () => {
    const uploads = new Map();
    const onUpload = vi.fn((file) => new Promise((resolve, reject) => {
      uploads.set(file.name, { resolve, reject });
    }));
    const onSend = vi.fn();
    const { container } = renderComposer({ onSend, onUpload });
    typeText("請整理這兩份");
    await pickFile(container, new File(["a"], "bad.txt", { type: "text/plain" }));
    await pickFile(container, new File(["b"], "good.txt", { type: "text/plain" }));

    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: BLOCKED }));
    expect(onSend).not.toHaveBeenCalled();

    await act(async () => {
      uploads.get("bad.txt").reject(new Error("bad.txt 上傳失敗"));
    });
    expect(screen.getByRole("alert").textContent).toContain("bad.txt 上傳失敗");
    expect(chip("bad.txt")).toBeNull();
    expect(chip("good.txt").textContent).toContain("上傳中");
    expect(screen.getByRole("button", { name: BLOCKED })).toBeDisabled();
    expect(screen.getByRole("textbox").value).toBe("請整理這兩份");

    await act(async () => {
      uploads.get("good.txt").resolve({
        filename: "good.txt",
        reference_id: "ref-good",
        content_type: "text/plain",
        size_bytes: 1,
        extract_status: "ok",
      });
    });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][1].map((att) => att.referenceId)).toEqual(["ref-good"]);
    expect(onSend.mock.calls[0][1].map((att) => att.name)).toEqual(["good.txt"]);
  });
});

describe("送出失敗後原文與附件回得來", () => {
  async function uploaded(container, onUploadWait, name, referenceId) {
    await pickFile(container, new File(["x"], name, { type: "text/plain" }));
    await act(async () => {
      onUploadWait.get(name).resolve({
        filename: name,
        reference_id: referenceId,
        content_type: "text/plain",
        size_bytes: 1,
        extract_status: "ok",
      });
    });
  }

  it("回 false 且框是空的：原文與 referenceId 放回輸入框", async () => {
    const uploads = new Map();
    const onUpload = vi.fn((file) => new Promise((resolve) => {
      uploads.set(file.name, { resolve });
    }));
    let settle;
    const onSend = vi.fn(() => new Promise((resolve) => {
      settle = resolve;
    }));
    const { container } = renderComposer({ onSend, onUpload });
    await uploaded(container, uploads, "note.txt", "ref-note");
    typeText("原始內容");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("textbox").value).toBe("");
    expect(chip("note.txt")).toBeNull();

    await act(async () => { settle(false); });
    expect(screen.getByRole("textbox").value).toBe("原始內容");
    expect(chip("note.txt")).toBeTruthy();
    expect(chip("note.txt").textContent).not.toContain("上傳中");
    expect(screen.queryByRole("button", { name: "還原未送出的訊息" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend).toHaveBeenCalledTimes(2);
    expect(onSend.mock.calls[1][0]).toBe("原始內容");
    expect(onSend.mock.calls[1][1][0].referenceId).toBe("ref-note");
    expect(onSend.mock.calls[0][1][0].referenceId).toBe("ref-note");
  });

  it("拒絕的 Promise 一樣放回原文與附件", async () => {
    const uploads = new Map();
    const onUpload = vi.fn((file) => new Promise((resolve) => {
      uploads.set(file.name, { resolve });
    }));
    let rejectSend;
    const onSend = vi.fn(() => new Promise((_resolve, reject) => {
      rejectSend = reject;
    }));
    const { container } = renderComposer({ onSend, onUpload });
    await uploaded(container, uploads, "note.txt", "ref-note");
    typeText("原始內容");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    await act(async () => { rejectSend(new Error("網路中斷")); });
    expect(screen.getByRole("textbox").value).toBe("原始內容");
    expect(chip("note.txt")).toBeTruthy();
  });

  it("框裡已有新內容時不覆蓋，還原會整份對調", async () => {
    const uploads = new Map();
    const onUpload = vi.fn((file) => new Promise((resolve) => {
      uploads.set(file.name, { resolve });
    }));
    let settle;
    const onSend = vi.fn(() => new Promise((resolve) => {
      settle = resolve;
    }));
    const { container } = renderComposer({ onSend, onUpload });
    await uploaded(container, uploads, "old.txt", "ref-old");
    typeText("原始內容");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(screen.getByRole("textbox").value).toBe("");

    typeText("新草稿");
    await uploaded(container, uploads, "new.txt", "ref-new");
    expect(chip("new.txt")).toBeTruthy();
    expect(chip("old.txt")).toBeNull();

    await act(async () => { settle(false); });
    expect(screen.getByRole("textbox").value).toBe("新草稿");
    expect(chip("new.txt")).toBeTruthy();
    expect(chip("old.txt")).toBeNull();
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("原始內容");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("old.txt");

    fireEvent.click(screen.getByRole("button", { name: "還原未送出的訊息" }));
    expect(screen.getByRole("textbox").value).toBe("原始內容");
    expect(chip("old.txt")).toBeTruthy();
    expect(chip("new.txt")).toBeNull();
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("新草稿");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("new.txt");

    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[1][0]).toBe("原始內容");
    expect(onSend.mock.calls[1][1].map((att) => att.referenceId)).toEqual(["ref-old"]);
    await act(async () => { onSend.mock.results[1].value.then?.(() => {}); });
  });

  it("模型把原文放回時，不蓋掉串流期間打的新草稿", () => {
    const { rerender } = renderComposer({ conversationId: 7 });
    typeText("新草稿");
    rerender({
      conversationId: 7,
      restoredDraft: { token: "restore-1", conversationId: 7, text: "原始內容" },
    });
    expect(screen.getByRole("textbox").value).toBe("新草稿");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("原始內容");
    fireEvent.click(screen.getByRole("button", { name: "還原未送出的訊息" }));
    expect(screen.getByRole("textbox").value).toBe("原始內容");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("新草稿");
  });

  it("輸入框是空的時，放回的原文直接回到框裡", () => {
    const { rerender } = renderComposer({ conversationId: 7 });
    rerender({
      conversationId: 7,
      restoredDraft: { token: "restore-2", conversationId: 7, text: "院內規章在哪" },
    });
    expect(screen.getByRole("textbox").value).toBe("院內規章在哪");
    expect(screen.queryByRole("button", { name: "還原未送出的訊息" })).toBeNull();
  });
});

const AUTOSEND = "請整理附件";

async function chooseAutosend(label) {
  await act(async () => {
    fireEvent.click(screen.getByTitle("預設提示詞"));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: new RegExp(label) }));
  });
}

describe("autosend 走受保護的送出", () => {
  it("附件還在上傳時不送出，也不清掉已打的字", async () => {
    const onUpload = vi.fn(() => new Promise(() => {}));
    const onSend = vi.fn();
    const { container } = renderComposer({
      onSend,
      onUpload,
      presetPrompts: [{ id: "p1", label: "整理", config: { text: AUTOSEND, autosend: true } }],
    });
    typeText("手打的字");
    await pickFile(container, new File(["hello"], "note.txt", { type: "text/plain" }));
    await chooseAutosend("整理");
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox").value).toBe("手打的字");
    expect(chip("note.txt").textContent).toContain("上傳中");
  });

  it("附件已上傳時一併送出，成功後才清掉輸入", async () => {
    const uploads = new Map();
    const onUpload = vi.fn((file) => new Promise((resolve) => {
      uploads.set(file.name, { resolve });
    }));
    const onSend = vi.fn();
    const { container } = renderComposer({
      onSend,
      onUpload,
      presetPrompts: [{ id: "p1", label: "整理", config: { text: AUTOSEND, autosend: true } }],
    });
    await pickFile(container, new File(["hello"], "note.txt", { type: "text/plain" }));
    await act(async () => {
      uploads.get("note.txt").resolve({
        filename: "note.txt",
        reference_id: "ref-note",
        content_type: "text/plain",
        size_bytes: 5,
        extract_status: "ok",
      });
    });
    await chooseAutosend("整理");
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0]).toBe(AUTOSEND);
    expect(onSend.mock.calls[0][1]).toEqual([
      expect.objectContaining({ name: "note.txt", referenceId: "ref-note", uploading: false }),
    ]);
    expect(screen.getByRole("textbox").value).toBe("");
    expect(chip("note.txt")).toBeNull();
  });

  it("送出被拒絕時把提示詞與附件放回，而不是直接清掉", async () => {
    const uploads = new Map();
    const onUpload = vi.fn((file) => new Promise((resolve) => {
      uploads.set(file.name, { resolve });
    }));
    let settle;
    const onSend = vi.fn(() => new Promise((resolve) => {
      settle = resolve;
    }));
    const { container } = renderComposer({
      onSend,
      onUpload,
      presetPrompts: [{ id: "p1", label: "整理", config: { text: AUTOSEND, autosend: true } }],
    });
    await pickFile(container, new File(["hello"], "note.txt", { type: "text/plain" }));
    await act(async () => {
      uploads.get("note.txt").resolve({
        filename: "note.txt",
        reference_id: "ref-note",
        content_type: "text/plain",
        size_bytes: 5,
        extract_status: "ok",
      });
    });
    await chooseAutosend("整理");
    expect(screen.getByRole("textbox").value).toBe("");
    expect(chip("note.txt")).toBeNull();
    await act(async () => { settle(false); });
    expect(screen.getByRole("textbox").value).toBe(AUTOSEND);
    expect(chip("note.txt")).toBeTruthy();
    expect(onSend.mock.calls[0][1][0].referenceId).toBe("ref-note");
  });
});

describe("新對話送出失敗不要當成切換", () => {
  it("失敗結果帶著這次建立的對話 id 時，原文回到那個對話", async () => {
    let settle;
    const onSend = vi.fn(() => new Promise((resolve) => {
      settle = resolve;
    }));
    const view = renderComposer({ onSend, conversationId: null });
    typeText("原始內容");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(screen.getByRole("textbox").value).toBe("");

    view.rerender({ conversationId: 42 });
    await act(async () => {
      settle({ ok: false, conversationId: 42 });
    });
    expect(screen.getByRole("textbox").value).toBe("原始內容");

    view.rerender({ conversationId: 99 });
    view.rerender({ conversationId: 42 });
    expect(screen.getByRole("textbox").value).toBe("原始內容");
  });

  it("失敗結果指的是另一個對話時，不把原文灌進目前這個對話", async () => {
    let settle;
    const onSend = vi.fn(() => new Promise((resolve) => {
      settle = resolve;
    }));
    const view = renderComposer({ onSend, conversationId: null });
    typeText("原始內容");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    view.rerender({ conversationId: 99 });
    await act(async () => {
      settle({ ok: false, conversationId: 42 });
    });
    expect(screen.getByRole("textbox").value).toBe("");
    expect(screen.queryByRole("button", { name: "還原未送出的訊息" })).toBeNull();
  });
});

describe("還原列按對話保留", () => {
  it("換去別的對話再回來，還沒捨棄的草稿與附件還在", async () => {
    const uploads = new Map();
    const onUpload = vi.fn((file) => new Promise((resolve) => {
      uploads.set(file.name, { resolve });
    }));
    let settle;
    const onSend = vi.fn(() => new Promise((resolve) => {
      settle = resolve;
    }));
    const view = renderComposer({ onSend, onUpload, conversationId: 7 });
    await pickFile(view.container, new File(["x"], "old.txt", { type: "text/plain" }));
    await act(async () => {
      uploads.get("old.txt").resolve({
        filename: "old.txt",
        reference_id: "ref-old",
        content_type: "text/plain",
        size_bytes: 1,
        extract_status: "ok",
      });
    });
    typeText("原始內容");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    typeText("新草稿");
    await pickFile(view.container, new File(["y"], "new.txt", { type: "text/plain" }));
    await act(async () => {
      uploads.get("new.txt").resolve({
        filename: "new.txt",
        reference_id: "ref-new",
        content_type: "text/plain",
        size_bytes: 1,
        extract_status: "ok",
      });
    });
    await act(async () => { settle(false); });
    fireEvent.click(screen.getByRole("button", { name: "還原未送出的訊息" }));
    expect(screen.getByRole("textbox").value).toBe("原始內容");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("新草稿");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("new.txt");

    view.rerender({ conversationId: 8 });
    expect(screen.queryByTestId("recoverable-draft")).toBeNull();
    expect(screen.getByRole("textbox").value).not.toContain("新草稿");

    view.rerender({ conversationId: 7 });
    expect(screen.getByRole("textbox").value).toBe("原始內容");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("新草稿");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("new.txt");
  });
});
