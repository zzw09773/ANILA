// 外流面 gate 的 UI 行為 —— 補救計畫 W1-1 ②⑥。
//
// 每一條都用**營業秘密**當案例:legacy `classified` boolean 對營業秘密是
// False,所以「吃 boolean」的舊寫法在營業秘密上會全綠。用絕對機密測則兩種
// 寫法都綠 —— 那種測試證明不了任何事。
//
// 姿態(N-3):被擋的動作**仍然渲染**,只是 disabled + tooltip。所以這裡的
// 斷言是「找得到、且 disabled、且 tooltip 有替代路徑」,不是「找不到」。

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import React from "react";
import { ConversationExportMenuItems, MessageBubble } from "../chat.jsx";

afterEach(cleanup);

const CONTROLLED = ["營業秘密", "機密", "極機密", "絕對機密"];

const conv = (level, extra = {}) => ({
  id: 1,
  title: "季度營收與客戶名單",
  classificationLevel: level,
  ...extra,
});

const assistantMsg = {
  id: "m1",
  role: "assistant",
  text: "客戶 A 的授權金是每年三百二十萬。",
  // 帶 citations 讓 bubble 走 renderTextWithCitations 而不是 MarkdownView
  // (避免在 jsdom 拉起 mermaid/katex 這些與本測試無關的重量級依賴)。
  citations: [{ id: "c1", title: "報價單", section: "§2" }],
};

// ── ② 匯出 gate ──────────────────────────────────────────────────────────────

describe("匯出入口:受控對話 disabled + tooltip(不是整條消失)", () => {
  it("營業秘密 —— 兩個匯出項都在、都 disabled、tooltip 含替代路徑", () => {
    const onExportConv = vi.fn();
    render(
      <ConversationExportMenuItems
        conversation={conv("營業秘密")}
        onExportConv={onExportConv}
      />,
    );
    const items = screen.getAllByRole("button");
    expect(items).toHaveLength(2);
    for (const item of items) {
      expect(item).toBeDisabled();
      expect(item.getAttribute("title")).toContain("替代路徑");
      expect(item.getAttribute("title")).toContain("營業秘密");
      // 措辭不得把營業秘密叫做「機密對話」
      expect(item.getAttribute("title")).not.toContain("機密對話禁止");
    }
    expect(screen.getByText(/Markdown/)).toBeTruthy();
    expect(screen.getByText(/JSON/)).toBeTruthy();
    expect(onExportConv).not.toHaveBeenCalled();
  });

  it("tooltip 回答「為何昨天能匯出今天不行」", () => {
    render(
      <ConversationExportMenuItems conversation={conv("營業秘密")} onExportConv={vi.fn()} />,
    );
    const title = screen.getAllByRole("button")[0].getAttribute("title");
    expect(title).toContain("昨天");
    expect(title).toContain("只升不降");
  });

  it("四個受控等級都擋,各自顯示自己的密等徽記", () => {
    for (const level of CONTROLLED) {
      render(
        <ConversationExportMenuItems conversation={conv(level)} onExportConv={vi.fn()} />,
      );
      for (const item of screen.getAllByRole("button")) {
        expect(item).toBeDisabled();
      }
      expect(screen.getAllByText(level).length).toBeGreaterThan(0);
      cleanup();
    }
  });

  it("無機密照舊可用(不得誤擋)", () => {
    const onExportConv = vi.fn();
    render(
      <ConversationExportMenuItems conversation={conv("無機密")} onExportConv={onExportConv} />,
    );
    const items = screen.getAllByRole("button");
    expect(items).toHaveLength(2);
    for (const item of items) expect(item).not.toBeDisabled();
    items[0].click();
    expect(onExportConv).toHaveBeenCalledWith(1, "markdown");
    items[1].click();
    expect(onExportConv).toHaveBeenCalledWith(1, "json");
  });

  it("legacy boolean 為 False 但密等是營業秘密 → 仍然擋(缺陷本體的回歸鎖)", () => {
    render(
      <ConversationExportMenuItems
        conversation={conv("營業秘密", { classified: false })}
        onExportConv={vi.fn()}
      />,
    );
    for (const item of screen.getAllByRole("button")) expect(item).toBeDisabled();
  });

  it("未知/損壞的密等 → fail-closed 擋下", () => {
    render(
      <ConversationExportMenuItems
        conversation={conv("他媽的什麼等級")}
        onExportConv={vi.fn()}
      />,
    );
    for (const item of screen.getAllByRole("button")) expect(item).toBeDisabled();
  });

  it("欄位缺漏且未 latch(本地新建對話)→ 不擋", () => {
    render(
      <ConversationExportMenuItems
        conversation={{ id: 2, title: "新對話" }}
        onExportConv={vi.fn()}
      />,
    );
    for (const item of screen.getAllByRole("button")) expect(item).not.toBeDisabled();
  });

  it("後端 snake_case payload(伺服器搜尋命中的列)也要擋", () => {
    // Sidebar 把伺服器搜尋命中映射成側欄列;那個映射原本只帶 legacy
    // `classified`(營業秘密是 False)→ 從搜尋出現的營業秘密對話匯出鈕是
    // 可按的。這條測試釘住「必須帶 classification_level」。
    render(
      <ConversationExportMenuItems
        conversation={{ id: 3, title: "季度營收", classified: false, classification_level: "營業秘密" }}
        onExportConv={vi.fn()}
      />,
    );
    for (const item of screen.getAllByRole("button")) expect(item).toBeDisabled();
  });
});

// ── ② 複製 gate ──────────────────────────────────────────────────────────────

describe("複製鈕:受控對話 disabled + tooltip", () => {
  const renderBubble = (props) =>
    render(
      <MessageBubble
        msg={assistantMsg}
        agents={[]}
        conversationId={1}
        {...props}
      />,
    );

  it("營業秘密 —— 複製鈕 disabled(舊碼吃 boolean,這裡是 False 所以會放行)", () => {
    renderBubble({ classified: false, classificationLevel: "營業秘密" });
    const btn = screen.getByTitle(/禁止複製/);
    expect(btn).toBeDisabled();
    expect(btn.getAttribute("title")).toContain("營業秘密");
    expect(btn.getAttribute("title")).toContain("替代路徑");
  });

  it("四個受控等級的複製都被擋", () => {
    for (const level of CONTROLLED) {
      renderBubble({ classified: false, classificationLevel: level });
      expect(screen.getByTitle(/禁止複製/)).toBeDisabled();
      cleanup();
    }
  });

  it("無機密的複製鈕照舊可用", () => {
    renderBubble({ classified: false, classificationLevel: "無機密" });
    expect(screen.queryByTitle(/禁止複製/)).toBeNull();
    expect(screen.getByTitle("複製")).toBeTruthy();
  });

  it("未知密等 fail-closed 擋下複製", () => {
    renderBubble({ classified: false, classificationLevel: "亂碼等級" });
    expect(screen.getByTitle(/禁止複製/)).toBeDisabled();
  });
});

// ── 伺服器搜尋命中的列(② 的第二個洞)──────────────────────────────────────
//
// Sidebar 會把 `/api/conversations/search` 的命中映射成側欄列。那個映射原本
// **只帶 legacy `classified`**,而它對營業秘密是 False —— 於是一條營業秘密對話
// 只要是「從搜尋出現的」,匯出鈕就是可按的。純元件測試(上面那條)釘住判定,
// 這條釘住**映射本身**沒有把密等丟掉。

describe("Sidebar:伺服器搜尋命中的營業秘密對話,匯出鈕仍為 disabled", () => {
  it("搜尋命中列帶 classification_level,匯出項 disabled", async () => {
    const { ConfirmProvider } = await import("../confirm.jsx");
    const { Sidebar } = await import("../chat.jsx");
    const hit = {
      id: 99,
      title: "季度營收與客戶名單",
      classified: false,
      classification_level: "營業秘密",
      updated_at: new Date().toISOString(),
      created_at: new Date().toISOString(),
      snippet: null,
    };
    render(
      <ConfirmProvider>
        <Sidebar
          conversations={[]}
          selectedConvId={null}
          onSelectConv={() => {}}
          onNewChat={() => {}}
          agents={[]}
          user={{ username: "alice" }}
          folder="all"
          setFolder={() => {}}
          folders={[]}
          onServerSearch={async () => [hit]}
          onExportConv={vi.fn()}
        />
      </ConfirmProvider>,
    );

    fireEvent.change(screen.getByPlaceholderText(/搜尋/), {
      target: { value: "營收" },
    });
    // Sidebar 對伺服器搜尋有 300ms debounce
    const rowTitle = await screen.findByText("季度營收與客戶名單", {}, { timeout: 3000 });
    expect(rowTitle).toBeTruthy();
    // 開啟該列的「更多」選單
    fireEvent.click(screen.getByTitle("更多"));
    const exportItems = screen.getAllByText(/匯出 (Markdown|JSON)/);
    expect(exportItems).toHaveLength(2);
    for (const label of exportItems) {
      expect(label.closest("button")).toBeDisabled();
    }
  });
});
