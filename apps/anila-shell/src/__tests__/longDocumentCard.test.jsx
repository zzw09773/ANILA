import React, { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { Composer, MessageBubble } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { LongDocumentPanelHost } from "../longDocumentCard.jsx";
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

function stubFetch(bodies) {
  const fetchMock = vi.fn(async (url) => {
    const hit = Object.entries(bodies).find(([id]) => String(url).includes(`/api/attachments/${id}`));
    if (!hit) throw new Error(String(url));
    return {
      ok: true,
      text: async () => hit[1],
      blob: async () => new Blob([hit[1]]),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  URL.createObjectURL = () => "blob:doc";
  URL.revokeObjectURL = () => {};
  return fetchMock;
}

function renderInShell(ui) {
  return render(
    <LongDocumentPanelHost>
      <div data-testid="chat-column" style={{ flex: 1, overflowY: "auto", minWidth: 0 }}>
        {ui}
      </div>
    </LongDocumentPanelHost>,
  );
}

function withinPanel(view, name) {
  const button = Array.from(view.querySelectorAll("button")).find((el) => el.textContent.trim() === name);
  if (!button) throw new Error(`側欄裡沒有「${name}」`);
  return button;
}

describe("長文文件卡", () => {
  it("訊息底下顯示標題、大小，以及開啟和下載", async () => {
    const fetchMock = stubFetch({
      "ref-1": "# 年度報告\n\n正文<script>alert(1)</script>",
    });

    renderInShell(
      <MessageBubble
        msg={assistant({ document: messageDocument({ document: card }) })}
        agents={[]}
        onContinue={() => {}}
        onCiteDocument={() => {}}
      />,
    );

    expect(screen.getByTestId("long-document-card").textContent).toContain("年度報告");
    expect(screen.getByTestId("long-document-card").textContent).toContain("KB");
    expect(screen.queryByRole("button", { name: "繼續" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "開啟" }));
    const view = await screen.findByTestId("long-document-view");
    const column = screen.getByTestId("chat-column");
    expect(column.contains(view)).toBe(false);
    expect(screen.getByTestId("long-document-card").contains(view)).toBe(false);
    expect(view.textContent).toContain("年度報告");
    expect(view.textContent).toContain("8 KB");
    expect(view.textContent).toContain("8,400");
    await waitFor(() => {
      expect(view.textContent).toContain("正文");
    });
    expect(screen.getByTestId("long-document-card").textContent).not.toContain("正文");
    expect(document.querySelector("script")).toBeNull();
    expect(withinPanel(view, "下載")).toBeTruthy();
    expect(withinPanel(view, "引用到訊息")).toBeTruthy();
    expect(withinPanel(view, "關閉")).toBeTruthy();

    fireEvent.click(withinPanel(view, "下載"));
    expect(fetchMock).toHaveBeenCalled();
    expect(String(fetchMock.mock.calls.at(-1)[0])).toContain("/api/attachments/ref-1");
  });

  it("關閉與 Escape 會關掉側欄，並把焦點還給開啟", async () => {
    stubFetch({ "ref-1": "# 年度報告\n\n正文" });
    renderInShell(
      <MessageBubble
        msg={assistant({ document: messageDocument({ document: card }) })}
        agents={[]}
      />,
    );
    const openBtn = screen.getByRole("button", { name: "開啟" });
    fireEvent.click(openBtn);
    const view = await screen.findByTestId("long-document-view");
    await waitFor(() => {
      expect(view.contains(document.activeElement) || document.activeElement === view).toBe(true);
    });

    fireEvent.click(withinPanel(view, "關閉"));
    expect(screen.queryByTestId("long-document-view")).toBeNull();
    expect(document.activeElement).toBe(openBtn);

    fireEvent.click(openBtn);
    await screen.findByTestId("long-document-view");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByTestId("long-document-view")).toBeNull();
    });
    expect(document.activeElement).toBe(openBtn);
  });

  it("再開另一份文件會換成那一份的內容", async () => {
    stubFetch({
      "ref-1": "# 年度報告\n\n第一份正文",
      "ref-2": "# 季度備忘\n\n第二份正文",
    });
    const second = {
      ...card,
      reference_id: "ref-2",
      filename: "季度備忘.md",
      size_bytes: 25600,
      char_count: 1200,
      title: "季度備忘",
    };
    renderInShell(
      <>
        <MessageBubble
          msg={assistant({ document: messageDocument({ document: card }) })}
          agents={[]}
        />
        <MessageBubble
          msg={assistant({
            id: "a2",
            document: messageDocument({ document: second }),
          })}
          agents={[]}
        />
      </>,
    );
    const [firstOpen, secondOpen] = screen.getAllByRole("button", { name: "開啟" });
    fireEvent.click(firstOpen);
    await screen.findByText("第一份正文");
    fireEvent.click(secondOpen);
    await screen.findByText("第二份正文");
    expect(screen.getAllByTestId("long-document-view")).toHaveLength(1);
    const view = screen.getByTestId("long-document-view");
    expect(view.textContent).toContain("25 KB");
    expect(view.textContent).not.toContain("第一份正文");
  });

  it("換對話會關掉側欄", async () => {
    stubFetch({ "ref-1": "# 年度報告\n\n正文" });
    const bubble = (
      <MessageBubble
        msg={assistant({ document: messageDocument({ document: card }) })}
        agents={[]}
      />
    );
    const { rerender } = render(
      <LongDocumentPanelHost resetKey="conv-1">{bubble}</LongDocumentPanelHost>,
    );
    fireEvent.click(screen.getByRole("button", { name: "開啟" }));
    await screen.findByTestId("long-document-view");
    rerender(<LongDocumentPanelHost resetKey="conv-2">{bubble}</LongDocumentPanelHost>);
    expect(screen.queryByTestId("long-document-view")).toBeNull();
  });

  it("引用到訊息把這份文件放進 composer，芯片顯示 25 KB 而不是 NaN", () => {
    const sized = { ...card, size_bytes: 25600 };
    function Harness() {
      const [queued, setQueued] = useState(null);
      return (
        <ConfirmProvider>
          <MessageBubble
            msg={assistant({ document: messageDocument({ document: sized }) })}
            agents={[]}
            onCiteDocument={setQueued}
          />
          <Composer
            onSend={vi.fn()}
            agents={[]}
            queuedAttachment={queued}
            onQueuedAttachmentConsumed={() => setQueued(null)}
          />
        </ConfirmProvider>
      );
    }
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "引用到訊息" }));
    const chip = document.querySelector(".composer-att-chip");
    expect(chip).toBeTruthy();
    expect(chip.textContent).toContain("25 KB");
    expect(chip.textContent).not.toMatch(/NaN/);
  });

  it("附件大小未知時芯片不顯示大小，也不出現 NaN", () => {
    render(
      <ConfirmProvider>
        <Composer
          onSend={vi.fn()}
          agents={[]}
          queuedAttachment={{ referenceId: "ref-x", name: "未量大小.md", kind: "file" }}
          onQueuedAttachmentConsumed={() => {}}
        />
      </ConfirmProvider>,
    );
    const chip = document.querySelector(".composer-att-chip");
    expect(chip).toBeTruthy();
    expect(chip.textContent).toContain("未量大小.md");
    expect(chip.textContent).not.toMatch(/NaN/);
    expect(chip.textContent).not.toMatch(/KB/);
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
