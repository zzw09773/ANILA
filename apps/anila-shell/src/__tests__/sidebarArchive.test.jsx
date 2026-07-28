// 側欄的封存(Archive)與標籤(Tag)組織功能。
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { Sidebar } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { DEFAULT_FOLDERS } from "../data.jsx";

const CONVERSATIONS = [
  { id: 1, title: "特休怎麼算", folder: "all", tags: ["hr"], updatedAt: "2026-07-24T10:00:00Z" },
  { id: 2, title: "封存的舊案", folder: "all", tags: [], archived: true, updatedAt: "2026-07-20T10:00:00Z" },
  { id: 3, title: "合約審查", folder: "all", tags: ["合約"], updatedAt: "2026-07-23T10:00:00Z" },
];

function setup(over = {}) {
  const onArchiveConv = vi.fn();
  const setFolder = vi.fn();
  const onOpenCommandPalette = vi.fn();
  const utils = render(
    <ConfirmProvider>
      <Sidebar
        conversations={CONVERSATIONS}
        selectedConvId={null}
        onSelectConv={() => {}}
        onNewChat={() => {}}
        agents={[]}
        user={{ username: "tester", role: "user" }}
        onLogout={() => {}}
        onOpenSettings={() => {}}
        collapsed={false}
        onToggleCollapsed={() => {}}
        folder="all"
        setFolder={setFolder}
        folders={DEFAULT_FOLDERS}
        onOpenTagEditor={() => {}}
        onRenameConv={() => {}}
        onDeleteConv={() => {}}
        onArchiveConv={onArchiveConv}
        onOpenCommandPalette={onOpenCommandPalette}
        {...over}
      />
    </ConfirmProvider>,
  );
  return { onArchiveConv, setFolder, onOpenCommandPalette, ...utils };
}

describe("Sidebar — 封存", () => {
  it("hides archived conversations from the main list", () => {
    setup();
    expect(screen.getByText("特休怎麼算")).toBeInTheDocument();
    expect(screen.getByText("合約審查")).toBeInTheDocument();
    expect(screen.queryByText("封存的舊案")).not.toBeInTheDocument();
  });

  it("offers an 已封存 entry with a count", () => {
    const { setFolder } = setup();
    const entry = screen.getByTitle("已封存的對話");
    expect(entry).toHaveTextContent("已封存 1");
    fireEvent.click(entry);
    expect(setFolder).toHaveBeenCalledWith("archived");
  });

  it("the 已封存 view shows only archived conversations", () => {
    setup({ folder: "archived" });
    expect(screen.getByText("封存的舊案")).toBeInTheDocument();
    expect(screen.queryByText("特休怎麼算")).not.toBeInTheDocument();
  });

  it("archives a single conversation from its row menu", () => {
    const { onArchiveConv } = setup();
    fireEvent.click(screen.getAllByTitle("更多")[0]);
    fireEvent.click(screen.getByText("封存對話"));
    expect(onArchiveConv).toHaveBeenCalledWith(1, true);
  });

  it("offers un-archive inside the 已封存 view", () => {
    const { onArchiveConv } = setup({ folder: "archived" });
    fireEvent.click(screen.getAllByTitle("更多")[0]);
    fireEvent.click(screen.getByText("取消封存"));
    expect(onArchiveConv).toHaveBeenCalledWith(2, false);
  });

  it("hides the archive action when the handler is absent (相容既有呼叫端)", () => {
    setup({ onArchiveConv: undefined });
    fireEvent.click(screen.getAllByTitle("更多")[0]);
    expect(screen.queryByText("封存對話")).not.toBeInTheDocument();
    expect(screen.getByText("重新命名")).toBeInTheDocument();
  });
});

describe("Sidebar — 標籤篩選", () => {
  it("renders a chip per tag and filters on click", () => {
    setup();
    const chip = screen.getByTitle("只看 #hr");
    expect(screen.getByTitle("只看 #合約")).toBeInTheDocument();
    fireEvent.click(chip);
    expect(screen.getByText("特休怎麼算")).toBeInTheDocument();
    expect(screen.queryByText("合約審查")).not.toBeInTheDocument();
    // 再點一次取消篩選
    fireEvent.click(screen.getByTitle("取消 #hr 篩選"));
    expect(screen.getByText("合約審查")).toBeInTheDocument();
  });

  it("renders no tag row when nothing is tagged", () => {
    setup({ conversations: [{ id: 9, title: "無標籤", folder: "all", tags: [] }] });
    expect(screen.queryByText("標籤")).not.toBeInTheDocument();
  });
});

describe("Sidebar — 命令面板入口", () => {
  it("exposes a 搜尋 / 跳轉 button wired to the palette", () => {
    const { onOpenCommandPalette } = setup();
    fireEvent.click(screen.getByText("搜尋 / 跳轉"));
    expect(onOpenCommandPalette).toHaveBeenCalledTimes(1);
  });
});
