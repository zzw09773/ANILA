import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { MessageBubble } from "../chat.jsx";
import { buildPersistMeta, messageDocument } from "../runtime/messageMeta.js";
import { dispatchSseEvent } from "../runtime/sse.js";
import { canContinueLengthReply } from "../runtime/reservedTurn.js";
import { applyDocumentToMessage } from "../runtime/longDocument.js";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const card = {
  reference_id: "ref-1",
  filename: "年度報告.md",
  size_bytes: 8192,
  char_count: 8400,
  title: "年度報告",
  preview: "這是開頭。\n\n目錄\n- 年度報告",
};

function assistant(extra) {
  return {
    id: "a1",
    role: "assistant",
    text: "這是開頭。\n\n目錄\n- 年度報告",
    streaming: false,
    finishReason: "length",
    ...extra,
  };
}

describe("長文文件卡", () => {
  it("訊息底下顯示標題、大小，以及開啟和下載", async () => {
    const fetchMock = vi.fn(async (url) => {
      if (!String(url).includes("/api/attachments/ref-1")) {
        throw new Error(String(url));
      }
      return {
        ok: true,
        text: async () => "# 年度報告\n\n正文<script>alert(1)</script>",
        blob: async () => new Blob(["# 年度報告"]),
      };
    });
    vi.stubGlobal("fetch", fetchMock);
    URL.createObjectURL = () => "blob:doc";
    URL.revokeObjectURL = () => {};

    render(
      <MessageBubble
        msg={assistant({ document: messageDocument({ document: card }) })}
        agents={[]}
        onContinue={() => {}}
      />,
    );

    expect(screen.getByTestId("long-document-card").textContent).toContain("年度報告");
    expect(screen.getByTestId("long-document-card").textContent).toContain("KB");
    expect(screen.queryByRole("button", { name: "繼續" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "開啟" }));
    expect(await screen.findByRole("heading", { name: "年度報告" })).toBeTruthy();
    expect(screen.getByTestId("long-document-view")).toBeTruthy();
    expect(document.querySelector("script")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "下載" }));
    expect(fetchMock).toHaveBeenCalled();
    expect(String(fetchMock.mock.calls.at(-1)[0])).toContain("/api/attachments/ref-1");
  });

  it("引用到訊息把這份文件放進 composer 用的附件", () => {
    const onCiteDocument = vi.fn();
    render(
      <MessageBubble
        msg={assistant({ document: messageDocument({ document: card }) })}
        agents={[]}
        onCiteDocument={onCiteDocument}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "引用到訊息" }));
    expect(onCiteDocument).toHaveBeenCalledWith({
      referenceId: "ref-1",
      name: "年度報告.md",
      kind: "file",
    });
  });

  it("沒有文件卡時，截斷的回答仍可繼續", () => {
    render(
      <MessageBubble
        msg={assistant({ text: "寫到一半", document: null })}
        agents={[]}
        onContinue={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "繼續" })).toBeTruthy();
    expect(canContinueLengthReply({ finishReason: "length", text: "寫到一半" })).toBe(true);
    expect(canContinueLengthReply({
      finishReason: "length",
      text: "寫到一半",
      document: { referenceId: "ref-1" },
    })).toBe(false);
  });

  it("文件卡寫進訊息並在重新載入時還原", () => {
    const event = { event: "anila.document", data: JSON.stringify(card) };
    const seen = [];
    dispatchSseEvent(event, { onDocument: (payload) => seen.push(payload) });
    expect(seen[0].reference_id).toBe("ref-1");

    const applied = applyDocumentToMessage(
      { text: "全文很長".repeat(100), finishReason: "length" },
      seen[0],
    );
    expect(applied.text).toBe(card.preview);
    expect(applied.document.referenceId).toBe("ref-1");
    expect(canContinueLengthReply(applied)).toBe(false);

    const persisted = buildPersistMeta(
      { document: card, finish_reason: "length" },
      { document: applied.document, finishReason: "length", text: applied.text },
    );
    expect(persisted.document.reference_id).toBe("ref-1");
    expect(persisted.document.filename).toBe("年度報告.md");

    const restored = messageDocument(persisted);
    expect(restored.title).toBe("年度報告");
    expect(restored.referenceId).toBe("ref-1");
    expect(restored.sizeBytes).toBe(8192);
    expect(restored.charCount).toBe(8400);
  });
});
