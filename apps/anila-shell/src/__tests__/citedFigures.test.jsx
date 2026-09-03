/**
 * Cited RAG hits that carry figure PKs must render those figures in the
 * answer, via the existing blob URL — never a data URI, never a second
 * channel that bypasses collection access.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import { MessageBubble } from "../chat.jsx";
import { fitLightboxBox, MarkdownView } from "../markdown.jsx";

afterEach(cleanup);

const CITED_WITH_FIGURE = [
  {
    id: "kb:7:21:1",
    title: "轉換區作業指南.pdf",
    snippet: "圖 1 轉換區示意圖",
    image_pks: [42],
  },
];

const CITED_NO_FIGURE = [
  {
    id: "kb:7:21:2",
    title: "人事管理規則.pdf",
    snippet: "第三條 差勤一律採線上簽核。",
  },
];

function assistantMsg(text, extra = {}) {
  return {
    id: 42,
    role: "assistant",
    text,
    streaming: false,
    siblingIndex: 0,
    siblingCount: 1,
    siblingIds: [42],
    citations: extra.citations,
    ...extra,
  };
}

function inlineChip(container, n) {
  return [...container.querySelectorAll("button")].find(
    (b) => b.textContent === `[${n}]`,
  );
}

function figureImgs(container) {
  return [...container.querySelectorAll("img")].filter((img) =>
    (img.getAttribute("src") || "").includes("/api/ingestion/images/"),
  );
}

describe("cited figures appear next to the citation chip", () => {
  it("renders the blob URL for a cited hit that has image_pks", () => {
    const { container } = render(
      <MessageBubble
        msg={assistantMsg("確有轉換區示意圖[1]。", {
          citations: CITED_WITH_FIGURE,
        })}
        agents={[]}
        conversationId={1}
        onOpenCitation={vi.fn()}
      />,
    );
    const body = container.querySelector(".anila-msg-body");
    expect(inlineChip(body, 1)).toBeTruthy();
    const imgs = figureImgs(body);
    expect(imgs.length).toBe(1);
    expect(imgs[0].getAttribute("src")).toBe("/api/ingestion/images/42/blob");
    expect(imgs[0].getAttribute("src")).not.toContain("data:");
  });

  it("clicking the figure opens the existing lightbox", () => {
    const { container } = render(
      <MarkdownView
        text={"見圖[1]"}
        citations={CITED_WITH_FIGURE}
        onOpenCitation={vi.fn()}
      />,
    );
    const img = figureImgs(container)[0];
    expect(img).toBeTruthy();
    fireEvent.click(img);
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).toBeTruthy();
    expect(dialog.querySelector("img")?.getAttribute("src")).toBe(
      "/api/ingestion/images/42/blob",
    );
  });

  // ⚠ 邊界：以下這幾格只斷言 style **字串**。
  // 元素盒與繪製區是否貼齊、暗底是否可點、右鍵是不是 IMG ——
  // **jsdom 量不到（無版面引擎，getBoundingClientRect() 全回 0），這裡一格都沒有守。**
  // 那一半在 `infra/checks/run-all.sh geometry`（Check 4，真瀏覽器）。
  // 🔴 不要把下面的 maxWidth／objectFit 斷言讀成「幾何行為由本檔守著」——
  // V-2（1535px 吞點擊死區）、R-1（右鍵另存被封）、A6-1（onLoad 前溢出 7.9 倍）
  // 三條全部通過了這裡的字串斷言，是真瀏覽器量出來才發現的。
  it("lightbox upscales a naturally small figure instead of only capping it", () => {
    // Defect: maxWidth/maxHeight alone never enlarge. A ~500px extraction
    // opened at the same size as the inline preview. width:min(92vw,…)
    // stretches it to the viewport; object-fit:contain keeps the ratio.
    // Blurry upscale is accepted — "can't enlarge" is the failure.
    const { container } = render(
      <MarkdownView
        text={"見圖[1]"}
        citations={CITED_WITH_FIGURE}
        onOpenCitation={vi.fn()}
      />,
    );
    fireEvent.click(figureImgs(container)[0]);
    const boxed = document.querySelector('[role="dialog"] img');
    expect(boxed).toBeTruthy();
    const style = boxed.getAttribute("style") || "";
    // A6-1: the first paint is width/height:auto. Without max-* a
    // 6000×1500 extraction overflows a 760×900 window 7.9×.
    expect(style).toMatch(/max-width:\s*92vw/i);
    expect(style).toMatch(/max-height:\s*92vh/i);
    expect(style).not.toMatch(/pointer-events:\s*none/i);
    expect(style).not.toMatch(/object-fit:\s*contain/i);
    expect(style).toMatch(/cursor:\s*default/i);
    expect(style).not.toMatch(/cursor:\s*zoom-out/i);
    const inline = figureImgs(container)[0];
    expect(inline.getAttribute("style") || "").not.toMatch(/max-width:\s*92vw/i);
  });

  it("dim backdrop still closes, and the img stays the contextmenu target", () => {
    // Both halves of R-1. pointer-events:none closed the letterbox but
    // made save-as hit the DIV. The box must equal the painted figure.
    const { container } = render(
      <MarkdownView
        text={"見圖[1]"}
        citations={CITED_WITH_FIGURE}
        onOpenCitation={vi.fn()}
      />,
    );
    fireEvent.click(figureImgs(container)[0]);
    const dialog = document.querySelector('[role="dialog"]');
    const boxed = dialog.querySelector("img");
    const boxedStyle = boxed.getAttribute("style") || "";
    expect(boxedStyle).not.toMatch(/pointer-events:\s*none/i);
    expect(boxedStyle).toMatch(/cursor:\s*default/i);
    expect(dialog.getAttribute("style") || "").toMatch(/cursor:\s*zoom-out/i);
    fireEvent.click(boxed);
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    fireEvent.contextMenu(boxed);
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    fireEvent.click(dialog);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("keeps the native aspect ratio when the viewport shrinks", () => {
    // R2-2: onLoad-only box + leftover max-vw/vh + no object-fit
    // squashed a 4:1 figure to 1.54:1. Mutant: drop the resize
    // listener *and* leave a CSS clamp — this numbers check still
    // fails if fitLightboxBox stops using min(axis limits).
    const wide = fitLightboxBox(2400, 600, 1977, 966);
    expect(wide.width / wide.height).toBeCloseTo(4, 1);
    const afterShrink = fitLightboxBox(2400, 600, 760, 900);
    expect(afterShrink.width / afterShrink.height).toBeCloseTo(4, 1);
    expect(afterShrink.width).toBeLessThanOrEqual(760 * 0.92 + 1);
    const tall = fitLightboxBox(600, 2400, 1920, 1080);
    expect(tall.width / tall.height).toBeCloseTo(0.25, 1);
    const tallShrunk = fitLightboxBox(600, 2400, 800, 500);
    expect(tallShrunk.width / tallShrunk.height).toBeCloseTo(0.25, 1);
  });

  it("caps the first paint before onLoad so a huge extraction cannot overflow", () => {
    const { container } = render(
      <MarkdownView
        text={"見圖[1]"}
        citations={CITED_WITH_FIGURE}
        onOpenCitation={vi.fn()}
      />,
    );
    fireEvent.click(figureImgs(container)[0]);
    const boxed = document.querySelector('[role="dialog"] img');
    const style = boxed.getAttribute("style") || "";
    expect(style).toMatch(/max-width:\s*92vw/i);
    expect(style).toMatch(/max-height:\s*92vh/i);
    expect(style).not.toMatch(/object-fit:\s*contain/i);
    // First frame is auto×auto + max-*. Mutant: drop either max-* → red.
  });

  it("cited hits without figures look like they do today (no img)", () => {
    const { container } = render(
      <MessageBubble
        msg={assistantMsg("差勤一律採線上簽核[1]。", {
          citations: CITED_NO_FIGURE,
        })}
        agents={[]}
        conversationId={1}
        onOpenCitation={vi.fn()}
      />,
    );
    const body = container.querySelector(".anila-msg-body");
    expect(inlineChip(body, 1)).toBeTruthy();
    expect(figureImgs(body)).toHaveLength(0);
    expect(body.querySelectorAll("img")).toHaveLength(0);
  });

  it("answers without citations grow no figure (regression)", () => {
    const { container } = render(
      <MessageBubble
        msg={assistantMsg("今天天氣不錯。")}
        agents={[]}
        conversationId={1}
      />,
    );
    const body = container.querySelector(".anila-msg-body");
    expect(figureImgs(body)).toHaveLength(0);
    expect(body.querySelectorAll("img")).toHaveLength(0);
  });

  it("renders a cited figure only once when the same chip repeats", () => {
    const citations = [
      { id: "kb:1", title: "other.pdf", snippet: "x" },
      {
        id: "kb:2",
        title: "強制裝備.pdf",
        snippet: "圖 2",
        image_pks: [315],
      },
    ];
    const { container } = render(
      <React.StrictMode>
        <MarkdownView
          text={"強制裝備[2]。安全救助[2]。"}
          citations={citations}
          onOpenCitation={vi.fn()}
        />
      </React.StrictMode>,
    );
    const chips = [...container.querySelectorAll("button")].filter(
      (b) => b.textContent === "[2]",
    );
    expect(chips).toHaveLength(2);
    const imgs = figureImgs(container);
    expect(imgs).toHaveLength(1);
    expect(imgs[0].getAttribute("src")).toBe("/api/ingestion/images/315/blob");
  });

  it("dedupes the same cited figure across multiple markdown blocks", () => {
    const citations = [
      { id: "kb:1", title: "other.pdf", snippet: "x" },
      {
        id: "kb:2",
        title: "強制裝備.pdf",
        snippet: "圖 2",
        image_pks: [315],
      },
    ];
    const { container } = render(
      <React.StrictMode>
        <MarkdownView
          text={"強制裝備[2]。\n\n安全救助[2]。"}
          citations={citations}
          onOpenCitation={vi.fn()}
        />
      </React.StrictMode>,
    );
    const chips = [...container.querySelectorAll("button")].filter(
      (b) => b.textContent === "[2]",
    );
    expect(chips).toHaveLength(2);
    const imgs = figureImgs(container);
    expect(imgs).toHaveLength(1);
    expect(imgs[0].getAttribute("src")).toBe("/api/ingestion/images/315/blob");
  });

  it("still renders one figure per distinct citation image", () => {
    const citations = [
      { id: "kb:1", title: "甲.pdf", snippet: "x", image_pks: [10] },
      { id: "kb:2", title: "乙.pdf", snippet: "y", image_pks: [315] },
    ];
    const { container } = render(
      <MarkdownView
        text={"甲[1]與乙[2]。"}
        citations={citations}
        onOpenCitation={vi.fn()}
      />,
    );
    const imgs = figureImgs(container);
    expect(imgs).toHaveLength(2);
    expect(imgs.map((img) => img.getAttribute("src"))).toEqual([
      "/api/ingestion/images/10/blob",
      "/api/ingestion/images/315/blob",
    ]);
    expect(inlineChip(container, 1)).toBeTruthy();
    expect(inlineChip(container, 2)).toBeTruthy();
  });

  it("a data URI on the citation object is not used as the image src", () => {
    const { container } = render(
      <MarkdownView
        text={"見圖[1]"}
        citations={[
          {
            ...CITED_WITH_FIGURE[0],
            data_uri: "data:image/png;base64,AAAA",
          },
        ]}
      />,
    );
    const imgs = figureImgs(container);
    expect(imgs).toHaveLength(1);
    expect(imgs[0].getAttribute("src")).toBe("/api/ingestion/images/42/blob");
    expect(container.innerHTML).not.toContain("data:image");
  });
});
