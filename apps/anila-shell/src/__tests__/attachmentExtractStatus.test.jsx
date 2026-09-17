// 不變式:「文字抽取終態失敗必須對使用者可見」。
//
// 擁有者確認的缺陷(2026-08-01):api.py 附上後 chip 看起來正常,模型沒看到檔案,
// 使用者只在模型道歉時才知道。這是專案 #1 禁止的失敗模式——控制項讓你以為
// 發生了什麼,其實沒有。
//
// 把 warning 渲染拿掉(data-extract-failed / 原因字串 / role=alert banner)
// 會讓本檔至少一條變紅。

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react";

import { Composer, COMPOSER_FILE_ACCEPT } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import {
  EXTRACT_STATUS_REASONS,
  extractStatusReason,
  pollAttachmentExtractStatus,
} from "../runtime/conversations.js";
import { ATTACHMENT_OVERFLOW_NOTICE } from "../runtime/messageAttachments.js";

const AGENTS = [{ id: "anila-router", name: "ANILA 自動選助手", short: "auto" }];

const INSTANT_POLL = {
  delaysMs: [0, 0, 0, 0, 0, 0],
  sleep: async () => {},
};

function renderComposer(props = {}) {
  return render(
    <ConfirmProvider>
      <Composer
        onSend={vi.fn()}
        agents={AGENTS}
        pollExtractOptions={INSTANT_POLL}
        {...props}
      />
    </ConfirmProvider>,
  );
}

function fileInput(container) {
  return container.querySelector('input[type="file"]');
}

async function pickFile(container, file) {
  const input = fileInput(container);
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } });
  });
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Backend-fixed actionable messages for extract_status=unsupported
// (anila_core ingestion text-class refusals). UI must show them verbatim.
const UNSUPPORTED_BACKEND_MESSAGES = [
  "此檔案似乎是 Big5 編碼的文字檔，目前僅支援 UTF-8／UTF-16。請另存為 UTF-8 後再上傳。",
  "此檔案可解讀為 UTF-16，但內容以非 ASCII 文字（例如中文）為主；UTF-16 僅支援以 ASCII 為主的檔案。請另存為 UTF-8 後再上傳。",
  "此檔案可解讀為 UTF-8，但內容無法可靠解讀為文字。本平台支援中文、英文、日文、韓文等一般文件；少數文字系統，以及大量使用組合符號或完全不含空白的長段落，目前可能無法解讀。請確認檔案編碼，或改以中文／英文提供內容後再上傳。",
  "此檔案不是可讀的文字檔（偵測到大量無效位元組）。",
];

describe("extractStatusReason / accept 清單", () => {
  it("終態失敗理由是繁中台灣用語", () => {
    expect(EXTRACT_STATUS_REASONS.unsupported).toBe("不支援的檔案格式");
    expect(EXTRACT_STATUS_REASONS.failed).toBe("解析失敗");
    expect(EXTRACT_STATUS_REASONS.too_large).toBe(ATTACHMENT_OVERFLOW_NOTICE);
    expect(EXTRACT_STATUS_REASONS.too_large).not.toMatch(/檢索/);
    expect(extractStatusReason("unsupported")).toBe("不支援的檔案格式");
    // 簡體對照字不得出現
    expect(EXTRACT_STATUS_REASONS.unsupported).not.toMatch(/文件|格式不支持/);
  });

  it("unsupported 原樣呈現後端 extract_error；空白／缺席回落通用標籤", () => {
    for (const msg of UNSUPPORTED_BACKEND_MESSAGES) {
      expect(extractStatusReason("unsupported", msg)).toBe(msg);
    }
    expect(extractStatusReason("unsupported")).toBe("不支援的檔案格式");
    expect(extractStatusReason("unsupported", null)).toBe("不支援的檔案格式");
    expect(extractStatusReason("unsupported", "")).toBe("不支援的檔案格式");
    expect(extractStatusReason("unsupported", "   ")).toBe("不支援的檔案格式");
  });

  it("too_large 固定用溢出句，不把 token 計數當 UI 文案", () => {
    expect(
      extractStatusReason("too_large", "抽取約 900000 tokens，超過單份附件儲存上限"),
    ).toBe(ATTACHMENT_OVERFLOW_NOTICE);
    expect(extractStatusReason("too_large")).toBe(ATTACHMENT_OVERFLOW_NOTICE);
    expect(extractStatusReason("too_large")).not.toMatch(/檢索/);
  });

  it("failed 不回傳後端 extract_error（路徑／模組名）", () => {
    const leaked = "/data/attachments/7/abc-uuid.pdf: ValueError in anila_core.parsers";
    expect(extractStatusReason("failed", leaked)).toBe("解析失敗");
    expect(extractStatusReason("failed", leaked)).not.toContain("/data/");
    expect(extractStatusReason("failed", leaked)).not.toContain("anila_core");
    expect(extractStatusReason("failed", leaked)).not.toContain(leaked);
    // 就算 extract_error 長得像 actionable unsupported 文案，failed 仍固定標籤。
    expect(
      extractStatusReason("failed", UNSUPPORTED_BACKEND_MESSAGES[0]),
    ).toBe("解析失敗");
    expect(
      extractStatusReason("failed", UNSUPPORTED_BACKEND_MESSAGES[0]),
    ).not.toContain("Big5");
  });

  it("檔案選擇器含後端已支援的文字／程式碼副檔名", () => {
    for (const ext of [".py", ".yaml", ".ts", ".go", ".rs", ".sql", ".json", ".pdf"]) {
      expect(COMPOSER_FILE_ACCEPT).toContain(ext);
    }
    expect(COMPOSER_FILE_ACCEPT).toContain("image/*");
  });
});

describe("pollAttachmentExtractStatus", () => {
  it("pending→unsupported 回傳終態", async () => {
    const fetchMeta = vi
      .fn()
      .mockResolvedValueOnce({ extract_status: "pending" })
      .mockResolvedValueOnce({ extract_status: "unsupported", extract_error: null });
    const out = await pollAttachmentExtractStatus(fetchMeta, "ref-1", INSTANT_POLL);
    expect(out).toEqual({
      status: "unsupported",
      extractError: null,
      budgetAdmitted: null,
      timedOut: false,
    });
  });

  it("一直 pending 則 timedOut,不捏造失敗", async () => {
    const fetchMeta = vi.fn().mockResolvedValue({ extract_status: "pending" });
    const out = await pollAttachmentExtractStatus(fetchMeta, "ref-1", INSTANT_POLL);
    expect(out.timedOut).toBe(true);
    expect(out.status).toBe("pending");
    expect(out.budgetAdmitted).toBeNull();
  });

  it("終態 ok 帶出 budget_admitted", async () => {
    const fetchMeta = vi.fn().mockResolvedValue({
      extract_status: "ok",
      extract_error: null,
      budget_admitted: false,
    });
    const out = await pollAttachmentExtractStatus(fetchMeta, "ref-big", INSTANT_POLL);
    expect(out).toEqual({
      status: "ok",
      extractError: null,
      budgetAdmitted: false,
      timedOut: false,
    });
  });

  it("網路錯誤不立刻當失敗", async () => {
    const fetchMeta = vi
      .fn()
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce({ extract_status: "ok", extract_error: null });
    const out = await pollAttachmentExtractStatus(fetchMeta, "ref-1", INSTANT_POLL);
    expect(out).toEqual({
      status: "ok",
      extractError: null,
      budgetAdmitted: null,
      timedOut: false,
    });
  });
});

describe("Composer — 抽取終態必須可見", () => {
  it("pending→unsupported：chip 警告狀態 + 原因,且 banner 觸發一次", async () => {
    // 拿掉 data-extract-failed="1"、失敗 class、或「不支援的檔案格式」渲染 → 這條變紅。
    const onFetchAttachmentMeta = vi
      .fn()
      .mockResolvedValueOnce({ extract_status: "pending" })
      .mockResolvedValueOnce({ extract_status: "unsupported", extract_error: null });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "api.py",
      reference_id: "ref-api",
      content_type: "text/x-python",
      size_bytes: 2048,
      extract_status: "pending",
    });

    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(container, new File(["x = 1\n"], "api.py", { type: "text/x-python" }));

    await waitFor(() => {
      const chip = container.querySelector('[data-extract-status="unsupported"]');
      expect(chip).not.toBeNull();
      expect(chip.getAttribute("data-extract-failed")).toBe("1");
      // 視覺警告不可被靜默 restyle 抹掉（class 或 border 信號）。
      expect(chip.classList.contains("composer-att-chip--extract-failed")).toBe(true);
      expect(chip.style.border).toMatch(/var\(--danger\)|danger/);
      expect(chip.textContent).toContain("不支援的檔案格式");
      expect(chip.textContent).toContain("api.py");
    });

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("api.py");
    expect(banner.textContent).toContain("不支援的檔案格式");

    // 送出不被擋(不新增限制):有附件時送出鈕仍可用。
    expect(screen.getByRole("button", { name: "送出" })).not.toBeDisabled();
  });

  it("unsupported + extract_error：chip／banner 原樣呈現後端可行動訊息", async () => {
    const backendMsg = UNSUPPORTED_BACKEND_MESSAGES[0];
    const onFetchAttachmentMeta = vi.fn().mockResolvedValue({
      extract_status: "unsupported",
      extract_error: backendMsg,
    });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "legacy.txt",
      reference_id: "ref-big5",
      content_type: "text/plain",
      size_bytes: 128,
      extract_status: "pending",
    });

    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(container, new File(["x"], "legacy.txt", { type: "text/plain" }));

    await waitFor(() => {
      const chip = container.querySelector('[data-extract-status="unsupported"]');
      expect(chip).not.toBeNull();
      expect(chip.getAttribute("data-extract-failed")).toBe("1");
      expect(chip.textContent).toContain(backendMsg);
      expect(chip.textContent).not.toContain("不支援的檔案格式");
    });

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("legacy.txt");
    expect(banner.textContent).toContain(backendMsg);
    expect(banner.textContent).not.toContain("不支援的檔案格式");
  });

  it("failed + 路徑樣 extract_error：chip／banner 只見固定標籤", async () => {
    const leaked = "/var/lib/anila/attachments/9/secret.pdf: RuntimeError in parsers.py";
    const onFetchAttachmentMeta = vi.fn().mockResolvedValue({
      extract_status: "failed",
      extract_error: leaked,
    });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "secret.pdf",
      reference_id: "ref-fail-leak",
      content_type: "application/pdf",
      size_bytes: 256,
      extract_status: "pending",
    });

    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(
      container,
      new File(["%PDF"], "secret.pdf", { type: "application/pdf" }),
    );

    await waitFor(() => {
      const chip = container.querySelector('[data-extract-status="failed"]');
      expect(chip).not.toBeNull();
      expect(chip.getAttribute("data-extract-failed")).toBe("1");
      expect(chip.textContent).toContain("解析失敗");
      expect(chip.textContent).not.toContain("/var/lib/");
      expect(chip.textContent).not.toContain("parsers.py");
      expect(chip.textContent).not.toContain(leaked);
    });

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("解析失敗");
    expect(banner.textContent).not.toContain("/var/lib/");
    expect(banner.textContent).not.toContain(leaked);
  });

  it("inline image（dataUrl）+ extract unsupported：不告失敗 chip、不開火 banner", async () => {
    // 不變式：client 已走 image_url 路徑送進模型的附件，不得因 extract_status
    // 被誣指失敗。拿掉 !dataUrl 閘門 → 這條變紅。
    const onFetchAttachmentMeta = vi
      .fn()
      .mockResolvedValueOnce({ extract_status: "pending" })
      .mockResolvedValueOnce({ extract_status: "unsupported", extract_error: null });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "icon.svg",
      reference_id: "ref-svg",
      content_type: "image/svg+xml",
      size_bytes: 64,
      extract_status: "pending",
    });

    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(
      container,
      new File(["<svg xmlns='http://www.w3.org/2000/svg'/>"], "icon.svg", {
        type: "image/svg+xml",
      }),
    );

    await waitFor(() => {
      const chip = container.querySelector('[data-extract-status="unsupported"]');
      expect(chip).not.toBeNull();
      expect(chip.getAttribute("data-extract-failed")).toBe("0");
      expect(chip.classList.contains("composer-att-chip--extract-failed")).toBe(false);
      expect(chip.style.border).not.toMatch(/var\(--danger\)|danger/);
      expect(chip.textContent).toContain("icon.svg");
      expect(chip.textContent).not.toContain("不支援的檔案格式");
      expect(chip.textContent).toMatch(/\d+\s*KB/);
    });

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("非 image（無 dataUrl）+ extract unsupported：仍警告 chip + banner（防過度抑制）", async () => {
    const onFetchAttachmentMeta = vi.fn().mockResolvedValue({
      extract_status: "unsupported",
      extract_error: null,
    });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "notes.pdf",
      reference_id: "ref-pdf",
      content_type: "application/pdf",
      size_bytes: 4096,
      extract_status: "pending",
    });

    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(
      container,
      new File(["%PDF-1.4"], "notes.pdf", { type: "application/pdf" }),
    );

    await waitFor(() => {
      const chip = container.querySelector('[data-extract-status="unsupported"]');
      expect(chip).not.toBeNull();
      expect(chip.getAttribute("data-extract-failed")).toBe("1");
      expect(chip.classList.contains("composer-att-chip--extract-failed")).toBe(true);
      expect(chip.style.border).toMatch(/var\(--danger\)|danger/);
      expect(chip.textContent).toContain("不支援的檔案格式");
      expect(chip.textContent).toContain("notes.pdf");
    });

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("notes.pdf");
    expect(banner.textContent).toContain("不支援的檔案格式");
  });

  it("pending→ok：正常 chip,不開火失敗 banner", async () => {
    const onFetchAttachmentMeta = vi
      .fn()
      .mockResolvedValueOnce({ extract_status: "pending" })
      .mockResolvedValueOnce({ extract_status: "ok", extract_error: null });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "notes.md",
      reference_id: "ref-ok",
      content_type: "text/markdown",
      size_bytes: 1024,
      extract_status: "pending",
    });

    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(container, new File(["# hi\n"], "notes.md", { type: "text/markdown" }));

    await waitFor(() => {
      const chip = container.querySelector('[data-extract-status="ok"]');
      expect(chip).not.toBeNull();
      expect(chip.getAttribute("data-extract-failed")).toBe("0");
      expect(chip.textContent).toContain("notes.md");
      expect(chip.textContent).toMatch(/\d+\s*KB/);
      expect(chip.textContent).not.toContain("不支援");
      expect(chip.textContent).not.toContain("解析失敗");
    });

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("輪詢逾時：不顯示假失敗,且與 confirmed-ok 可區分", async () => {
    const onFetchAttachmentMeta = vi.fn().mockResolvedValue({ extract_status: "pending" });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "slow.log",
      reference_id: "ref-slow",
      content_type: "text/plain",
      size_bytes: 512,
      extract_status: "pending",
    });

    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(container, new File(["log\n"], "slow.log", { type: "text/plain" }));

    await waitFor(() => {
      expect(onFetchAttachmentMeta.mock.calls.length).toBeGreaterThan(1);
      const chip = container.querySelector("[data-extract-status]");
      expect(chip).not.toBeNull();
      expect(chip.getAttribute("data-extract-failed")).toBe("0");
      // 禁止假失敗宣稱；逾時後必須仍可見地不確定（不可掉成純 KB＝confirmed-ok）。
      expect(chip.textContent).not.toContain("不支援的檔案格式");
      expect(chip.textContent).not.toContain("解析失敗");
      expect(chip.textContent).not.toContain(ATTACHMENT_OVERFLOW_NOTICE);
      expect(chip.textContent).toContain("狀態未知");
      expect(chip.textContent).not.toMatch(/\d+\s*KB/);
      expect(chip.getAttribute("data-extract-uncertain")).toBe("1");
    });

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("多檔：全部上傳不等待輪詢,結果依 referenceId 落在正確 chip", async () => {
    const gates = {};
    const onFetchAttachmentMeta = vi.fn(
      (refId) =>
        new Promise((resolve) => {
          gates[refId] = resolve;
        }),
    );
    const onUpload = vi.fn().mockImplementation(async (file) => ({
      filename: file.name,
      reference_id: file.name === "a.txt" ? "ref-a" : "ref-b",
      content_type: "text/plain",
      size_bytes: file.name === "a.txt" ? 100 : 200,
      extract_status: "pending",
    }));

    const { container } = renderComposer({
      onUpload,
      onFetchAttachmentMeta,
      pollExtractOptions: { delaysMs: [0, 0, 0], sleep: async () => {} },
    });
    const input = fileInput(container);
    await act(async () => {
      fireEvent.change(input, {
        target: {
          files: [
            new File(["aaa"], "a.txt", { type: "text/plain" }),
            new File(["bbb"], "b.txt", { type: "text/plain" }),
          ],
        },
      });
    });

    // 兩個上傳都已發出，且在任一輪詢完成前沒有 chip 卡在上傳中。
    await waitFor(() => {
      expect(onUpload).toHaveBeenCalledTimes(2);
      expect(Object.keys(gates).sort()).toEqual(["ref-a", "ref-b"]);
      const chips = [...container.querySelectorAll("[data-extract-status]")];
      expect(chips).toHaveLength(2);
      expect(chips.every((c) => c.getAttribute("data-extract-status") !== "uploading")).toBe(true);
      expect(chips.every((c) => c.textContent.includes("處理中"))).toBe(true);
    });

    await act(async () => {
      gates["ref-a"]({ extract_status: "unsupported", extract_error: null });
      gates["ref-b"]({ extract_status: "ok", extract_error: null });
    });

    await waitFor(() => {
      const chipA = [...container.querySelectorAll("[data-extract-status]")].find((c) =>
        c.textContent.includes("a.txt"),
      );
      const chipB = [...container.querySelectorAll("[data-extract-status]")].find((c) =>
        c.textContent.includes("b.txt"),
      );
      expect(chipA?.getAttribute("data-extract-status")).toBe("unsupported");
      expect(chipA?.textContent).toContain("不支援的檔案格式");
      expect(chipB?.getAttribute("data-extract-status")).toBe("ok");
      expect(chipB?.textContent).toMatch(/\d+\s*KB/);
      expect(chipB?.textContent).not.toContain("不支援");
    });
  });

  it("多檔：最後一個失敗的檔仍必須開火 banner（N1）", async () => {
    // 回歸：concurrent setAtts 會讓 updater 延遲執行；若用 updater 外讀
    // stillLive，fail-LAST 的 banner 會被靜默丟掉。chip 變紅不算過關。
    const gates = {};
    const onFetchAttachmentMeta = vi.fn(
      (refId) =>
        new Promise((resolve) => {
          gates[refId] = resolve;
        }),
    );
    const onUpload = vi.fn().mockImplementation(async (file) => ({
      filename: file.name,
      reference_id: `ref-${file.name}`,
      content_type: "text/plain",
      size_bytes: 50,
      extract_status: "pending",
    }));

    const { container } = renderComposer({
      onUpload,
      onFetchAttachmentMeta,
      pollExtractOptions: { delaysMs: [0, 0, 0], sleep: async () => {} },
    });
    const input = fileInput(container);
    await act(async () => {
      fireEvent.change(input, {
        target: {
          files: [
            new File(["ok1"], "ok1.txt", { type: "text/plain" }),
            new File(["ok2"], "ok2.txt", { type: "text/plain" }),
            new File(["bad"], "bad.txt", { type: "text/plain" }),
          ],
        },
      });
    });

    await waitFor(() => {
      expect(Object.keys(gates).sort()).toEqual([
        "ref-bad.txt",
        "ref-ok1.txt",
        "ref-ok2.txt",
      ]);
    });

    // Healthy siblings resolve first so their setAtts queue pending lanes;
    // failing file resolves LAST (reviewer probe: fail-LAST of N → MISSING).
    await act(async () => {
      gates["ref-ok1.txt"]({ extract_status: "ok", extract_error: null });
      gates["ref-ok2.txt"]({ extract_status: "ok", extract_error: null });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      gates["ref-bad.txt"]({
        extract_status: "unsupported",
        extract_error: null,
      });
    });

    await waitFor(() => {
      const chipBad = [...container.querySelectorAll("[data-extract-status]")].find((c) =>
        c.textContent.includes("bad.txt"),
      );
      expect(chipBad?.getAttribute("data-extract-failed")).toBe("1");
      expect(chipBad?.textContent).toContain("不支援的檔案格式");
    });

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("bad.txt");
    expect(banner.textContent).toContain("不支援的檔案格式");
  });

  it("輪詢失敗時若 chip 已移除則不開火 banner", async () => {
    let resolveMeta;
    const onFetchAttachmentMeta = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveMeta = resolve;
        }),
    );
    const onUpload = vi.fn().mockResolvedValue({
      filename: "gone.txt",
      reference_id: "ref-gone",
      content_type: "text/plain",
      size_bytes: 40,
      extract_status: "pending",
    });

    const { container } = renderComposer({
      onUpload,
      onFetchAttachmentMeta,
      pollExtractOptions: { delaysMs: [0], sleep: async () => {} },
    });
    await pickFile(container, new File(["x"], "gone.txt", { type: "text/plain" }));

    await waitFor(() => {
      const chip = container.querySelector('[data-extract-status="pending"]');
      expect(chip).not.toBeNull();
      expect(chip.textContent).toContain("gone.txt");
    });

    const chip = container.querySelector('[data-extract-status="pending"]');
    fireEvent.click(chip.querySelector("button"));
    expect(container.querySelector("[data-extract-status]")).toBeNull();

    await act(async () => {
      resolveMeta({ extract_status: "failed", extract_error: null });
    });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("failed／too_large 也走警告 chip + banner", async () => {
    for (const [status, reason] of [
      ["failed", "解析失敗"],
      ["too_large", ATTACHMENT_OVERFLOW_NOTICE],
    ]) {
      cleanup();
      const onFetchAttachmentMeta = vi.fn().mockResolvedValue({
        extract_status: status,
        extract_error: null,
      });
      const onUpload = vi.fn().mockResolvedValue({
        filename: `f-${status}.bin`,
        reference_id: `ref-${status}`,
        content_type: "application/octet-stream",
        size_bytes: 100,
        extract_status: "pending",
      });
      const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
      await pickFile(
        container,
        new File(["x"], `f-${status}.bin`, { type: "application/octet-stream" }),
      );
      await waitFor(() => {
        const chip = container.querySelector(`[data-extract-status="${status}"]`);
        expect(chip?.getAttribute("data-extract-failed")).toBe("1");
        expect(chip?.textContent).toContain(reason);
      });
      expect((await screen.findByRole("alert")).textContent).toContain(reason);
    }
  });

  it("任何抽取狀態下移除鈕都還在", async () => {
    const onFetchAttachmentMeta = vi.fn().mockResolvedValue({
      extract_status: "unsupported",
      extract_error: null,
    });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "bad.py",
      reference_id: "ref-rm",
      content_type: "text/x-python",
      size_bytes: 10,
      extract_status: "pending",
    });
    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(container, new File(["x"], "bad.py", { type: "text/x-python" }));

    await waitFor(() => {
      expect(container.querySelector('[data-extract-failed="1"]')).not.toBeNull();
    });
    const chip = container.querySelector('[data-extract-failed="1"]');
    const removeBtn = chip.querySelector("button");
    expect(removeBtn).not.toBeNull();
    fireEvent.click(removeBtn);
    expect(container.querySelector('[data-extract-failed="1"]')).toBeNull();
  });

  it("extract ok 但 budget 未納入：警告 chip + 溢出句，不當抽取失敗", async () => {
    const onFetchAttachmentMeta = vi.fn().mockResolvedValue({
      extract_status: "ok",
      extract_error: null,
      budget_admitted: false,
    });
    const onUpload = vi.fn().mockResolvedValue({
      filename: "huge.pdf",
      reference_id: "ref-budget",
      content_type: "application/pdf",
      size_bytes: 4096,
      extract_status: "pending",
    });
    const { container } = renderComposer({ onUpload, onFetchAttachmentMeta });
    await pickFile(
      container,
      new File(["%PDF-1.4"], "huge.pdf", { type: "application/pdf" }),
    );
    await waitFor(() => {
      const chip = container.querySelector('[data-extract-status="ok"]');
      expect(chip).not.toBeNull();
      expect(chip.getAttribute("data-extract-failed")).toBe("0");
      expect(chip.getAttribute("data-extract-overflow")).toBe("1");
      expect(chip.classList.contains("composer-att-chip--overflow")).toBe(true);
      expect(chip.textContent).toContain(ATTACHMENT_OVERFLOW_NOTICE);
      expect(chip.textContent).not.toMatch(/檢索/);
    });
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("huge.pdf");
    expect(banner.textContent).toContain(ATTACHMENT_OVERFLOW_NOTICE);
    expect(banner.getAttribute("data-testid")).toBe("attachment-overflow-notice");
  });
});
