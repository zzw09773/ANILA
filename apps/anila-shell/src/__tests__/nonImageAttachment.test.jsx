// 非圖片附件的靜默幻覺止血 —— 補救計畫 W1-2。
//
// 缺陷:聊天路徑沒有接任何 parser。使用者拖一份 PDF 進來 → 附件上傳成功、chip
// 顯示成功、送出成功,而 `buildUserContent` **只把檔名**塞進 prompt:
//
//     `${text}\n\n[附件]\n- 季度報表.pdf`
//
// 模型收到的是一個檔名,於是它對一份自己從來沒看過的文件產生幻覺 —— 而 UI
// 全程顯示成功,使用者沒有任何線索知道內容根本沒送出去。這比「明確失敗」糟得多。
//
// ≥10MB 的圖片走同一條路:`chat.jsx` 的條件是
// `startsWith("image/") && file.size < 10MB`,不符合就 `dataUrl = null` →
// 掉進 otherFiles → 同樣只剩檔名。**靜默降級,零提示。**
//
// 本包是止血(擋下 + 導引),不是真修。真修 = 接 parser 注入 = W3-9。
//
// 為什麼止血也要做:靜默幻覺是「看起來有在工作」的失敗,使用者會拿模型編出來的
// 內容當真。明確擋下並指出替代路徑,比一個好看但錯的答案有價值。

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import React from "react";
import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import {
  buildUserContent,
  isModelReadableAttachment,
  MAX_INLINE_IMAGE_BYTES,
} from "../runtime/userContent.js";

afterEach(cleanup);

const img = (extra = {}) => ({
  name: "chart.png",
  kind: "image",
  contentType: "image/png",
  dataUrl: "data:image/png;base64,AAAA",
  ...extra,
});

// ── buildUserContent:不得讓模型以為它拿到了內容 ──────────────────────────────

describe("buildUserContent", () => {
  it("非圖片附件不得只送檔名就了事(舊行為 = 靜默幻覺)", () => {
    const out = buildUserContent("幫我摘要", [{ name: "季度報表.pdf" }]);
    const text = typeof out === "string" ? out : out[0].text;
    // 檔名可以在(讓模型知道有人附了東西),但**必須同時說清楚內容沒送出**,
    // 否則模型會假裝讀過。這一條就是防「看起來有在工作」的失敗。
    expect(text).toContain("季度報表.pdf");
    expect(text).toMatch(/內容(沒有|未)送/);
    expect(text).toContain("我的知識庫");
  });

  it("純文字無附件時原封不動", () => {
    expect(buildUserContent("你好", [])).toBe("你好");
  });

  it("圖片仍走 image_url,行為不變", () => {
    const out = buildUserContent("這張圖說什麼", [img()]);
    expect(Array.isArray(out)).toBe(true);
    expect(out[1]).toEqual({
      type: "image_url",
      image_url: { url: "data:image/png;base64,AAAA" },
    });
  });

  it("圖片缺 dataUrl(超過內嵌上限被擋)時,不得偽裝成看得到圖", () => {
    const out = buildUserContent("這張圖說什麼", [img({ dataUrl: null })]);
    const text = typeof out === "string" ? out : out[0].text;
    expect(text).toMatch(/內容(沒有|未)送/);
  });
});

describe("isModelReadableAttachment", () => {
  it("小圖可讀", () => {
    expect(isModelReadableAttachment({ type: "image/png", size: 1024 })).toBe(true);
  });
  it("超過內嵌上限的圖不可讀(而且不是靜默降級,呼叫端要據此報錯)", () => {
    expect(
      isModelReadableAttachment({ type: "image/png", size: MAX_INLINE_IMAGE_BYTES + 1 }),
    ).toBe(false);
  });
  it("PDF 不可讀", () => {
    expect(isModelReadableAttachment({ type: "application/pdf", size: 10 })).toBe(false);
  });
  it("沒有 type 的檔案不可讀(fail-closed,不要猜)", () => {
    expect(isModelReadableAttachment({ name: "x.bin", size: 10 })).toBe(false);
  });
});

// ── Composer:擋在上傳之前,並給替代路徑 ─────────────────────────────────────

function pick(file) {
  const onUpload = vi.fn().mockResolvedValue({
    filename: file.name,
    size_bytes: file.size,
    reference_id: "ref-1",
    content_type: file.type,
  });
  render(
    <ConfirmProvider>
      <Composer
        value=""
        onChange={() => {}}
        onSubmit={() => {}}
        onUpload={onUpload}
      />
    </ConfirmProvider>,
  );
  const input = document.querySelector('input[type="file"]');
  expect(input).toBeTruthy();
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  fireEvent.change(input);
  return { onUpload };
}

function fakeFile(name, type, size) {
  const f = new File(["x"], name, { type });
  Object.defineProperty(f, "size", { value: size });
  return f;
}

describe("Composer 擋下模型讀不到的附件", () => {
  it("PDF:不上傳、不建 chip、顯示導引與替代路徑", async () => {
    const { onUpload } = pick(fakeFile("季度報表.pdf", "application/pdf", 2048));
    // 最關鍵的一條:**根本不要上傳**。上傳成功的 chip 就是「看起來有在工作」的來源。
    expect(onUpload).not.toHaveBeenCalled();
    const notice = await screen.findByText(/我的知識庫/);
    expect(notice).toBeTruthy();
    expect(notice.textContent).toMatch(/季度報表\.pdf/);
    expect(screen.queryByTitle(/移除附件/)).toBeNull();
  });

  it("≥10MB 圖片:顯式錯誤,不靜默降級成只送檔名", async () => {
    const { onUpload } = pick(
      fakeFile("大圖.png", "image/png", MAX_INLINE_IMAGE_BYTES + 1),
    );
    expect(onUpload).not.toHaveBeenCalled();
    const notice = await screen.findByText(/大圖\.png/);
    expect(notice.textContent).toMatch(/10 ?MB|上限/);
  });

  it("小圖照舊可用 —— 不得誤擋", async () => {
    const { onUpload } = pick(fakeFile("小圖.png", "image/png", 1024));
    await vi.waitFor(() => expect(onUpload).toHaveBeenCalledTimes(1));
  });
});
