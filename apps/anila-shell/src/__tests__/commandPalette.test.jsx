// 命令面板(⌘K)與快捷鍵面板(⌘/)的渲染 / 鍵盤操作 / 前綴過濾。
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { CommandPalette } from "../commands/CommandPalette.jsx";
import { ShortcutsPanel } from "../commands/ShortcutsPanel.jsx";
import { SHORTCUTS } from "../commands/shortcuts.js";
import { ConfidentialWatermark } from "../trust.jsx";

const CONVERSATIONS = [
  { id: 1, title: "特休怎麼算", folder: "usr-hr", tags: ["hr"], updatedAt: "2026-07-24T10:00:00Z" },
  { id: 2, title: "封存的舊案", archived: true, tags: [], updatedAt: "2026-07-20T10:00:00Z" },
  { id: 3, title: "合約審查", folder: "usr-legal", tags: ["合約"], starred: true, updatedAt: "2026-07-23T10:00:00Z" },
];

function setup(over = {}) {
  const onSelectConv = vi.fn();
  const onClose = vi.fn();
  const run = vi.fn();
  const actions = [{ id: "new-chat", label: "新對話", keywords: ["new"], run }];
  const utils = render(
    <CommandPalette
      open
      onClose={onClose}
      conversations={CONVERSATIONS}
      folders={[{ id: "usr-legal", name: "法務" }]}
      actions={actions}
      onSelectConv={onSelectConv}
      {...over}
    />,
  );
  return { onSelectConv, onClose, run, input: screen.getByLabelText("搜尋對話或跳轉動作"), ...utils };
}

describe("CommandPalette", () => {
  it("renders nothing while closed", () => {
    const { container } = render(<CommandPalette open={false} conversations={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists actions and non-archived conversations by default", () => {
    setup();
    expect(screen.getByText("新對話")).toBeInTheDocument();
    expect(screen.getByText("特休怎麼算")).toBeInTheDocument();
    expect(screen.getByText("合約審查")).toBeInTheDocument();
    expect(screen.queryByText("封存的舊案")).not.toBeInTheDocument();
  });

  it("finds archived conversations through free-text search", () => {
    const { input } = setup();
    fireEvent.change(input, { target: { value: "封存" } });
    expect(screen.getByText("封存的舊案")).toBeInTheDocument();
    expect(screen.getByText("已封存")).toBeInTheDocument();
  });

  it("archived: shows only archived conversations and drops the action rows", () => {
    const { input } = setup();
    fireEvent.change(input, { target: { value: "archived:" } });
    expect(screen.getByText("封存的舊案")).toBeInTheDocument();
    expect(screen.queryByText("特休怎麼算")).not.toBeInTheDocument();
    expect(screen.queryByText("新對話")).not.toBeInTheDocument();
  });

  it("supports tag: / starred: / folder: prefixes", () => {
    const { input } = setup();
    fireEvent.change(input, { target: { value: "tag:hr" } });
    expect(screen.getByText("特休怎麼算")).toBeInTheDocument();
    expect(screen.queryByText("合約審查")).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: "starred:" } });
    expect(screen.getByText("合約審查")).toBeInTheDocument();
    expect(screen.queryByText("特休怎麼算")).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: "folder:法務" } });
    expect(screen.getByText("合約審查")).toBeInTheDocument();
    expect(screen.queryByText("特休怎麼算")).not.toBeInTheDocument();
  });

  it("Enter opens the highlighted row", () => {
    const { input, onSelectConv, onClose, run } = setup();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(run).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "特休" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSelectConv).toHaveBeenCalledWith(1);
  });

  it("ArrowDown moves the selection before Enter", () => {
    const { input, onSelectConv } = setup();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    // rows: [action 新對話, conv 特休(較新), conv 合約審查]
    expect(onSelectConv).toHaveBeenCalledWith(1);
  });

  it("Escape closes", () => {
    const { input, onClose } = setup();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("shows prefix hints when nothing matches", () => {
    const { input } = setup();
    fireEvent.change(input, { target: { value: "完全不存在的東西" } });
    expect(screen.getByText("沒有符合的結果")).toBeInTheDocument();
    expect(screen.getByText(/folder:/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 必修 2:面板 z-index 120 壓過原本 z-index 4 的鑑識浮水印,於是面板裡的
// classified 標題落在「沒有使用者 / trace 歸屬」的圖層。涉密內容不得出現在
// 無浮水印的圖層 → 面板列出 classified 對話時,面板內自己重繪浮水印。
// ---------------------------------------------------------------------------
describe("CommandPalette — 涉密內容的鑑識浮水印", () => {
  const CLASSIFIED = [
    { id: 5, title: "機密專案", classified: true, classificationLevel: "極機密", updatedAt: "2026-07-24T10:00:00Z" },
  ];

  it("列出 classified 對話時,面板內重繪帶歸屬資訊的浮水印", () => {
    render(
      <CommandPalette
        open
        onClose={() => {}}
        conversations={CLASSIFIED}
        folders={[]}
        actions={[]}
        onSelectConv={() => {}}
        watermarkUser="tester@ncsist.org.tw"
        watermarkTraceId="trace-abc"
      />,
    );
    expect(screen.getByText("機密專案")).toBeInTheDocument();
    expect(
      screen.getByText("極機密 · tester@ncsist.org.tw · trace-abc", { exact: false }),
    ).toBeInTheDocument();
  });

  it("沒有 classified 對話時不畫浮水印(不干擾一般使用)", () => {
    render(
      <CommandPalette
        open
        onClose={() => {}}
        conversations={CONVERSATIONS}
        folders={[]}
        actions={[]}
        onSelectConv={() => {}}
        watermarkUser="tester@ncsist.org.tw"
      />,
    );
    expect(screen.queryByText(/tester@ncsist\.org\.tw/)).not.toBeInTheDocument();
  });

  it("浮水印的 z-index 高於面板本身(全域那一層不會被面板蓋掉)", () => {
    const { container } = render(
      <ConfidentialWatermark userEmail="tester" traceId="t1" level="機密" />,
    );
    const el = container.firstChild;
    // 120 = 命令面板;100 = --anila-z-modal;200 = 密等橫幅(必須仍在最上面)。
    expect(Number(el.style.zIndex)).toBeGreaterThan(120);
    expect(Number(el.style.zIndex)).toBeLessThan(200);
  });
});

describe("ShortcutsPanel", () => {
  it("renders every registered shortcut with a cross-platform key label", () => {
    render(<ShortcutsPanel open onClose={() => {}} />);
    for (const shortcut of SHORTCUTS) {
      expect(screen.getByText(shortcut.description)).toBeInTheDocument();
    }
    // 面板內容全由 registry 產生 → 群組標題也在
    expect(screen.getByText("全域")).toBeInTheDocument();
    expect(screen.getByText("輸入框")).toBeInTheDocument();
  });

  it("renders nothing while closed", () => {
    const { container } = render(<ShortcutsPanel open={false} onClose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
