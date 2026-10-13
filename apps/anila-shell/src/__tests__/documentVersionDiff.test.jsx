import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ShellNav } from "../shellNav.jsx";
import { DocumentDiffCards, DocumentVersionCompare } from "../documentVersionDiff.jsx";
import { visibleExplanations } from "../runtime/documentDiff.js";

const diff = {
  parse_complete: true,
  readable: true,
  summary: "依條號比對，找到 2 處差異。",
  warnings: [],
  changes: [
    {
      id: "diff:1:modified",
      kind: "modified",
      article: "第一條",
      importance: "high",
      before: "第一條\n茶會在週三。",
      after: "第一條\n茶會在週四。",
      summary: "修改第一條",
    },
    {
      id: "diff:2:modified",
      kind: "modified",
      article: "第二條",
      importance: "low",
      before: "第二條\n座位先到先坐。",
      after: "第二條\n座位  先到先坐。",
      summary: "修改第二條",
    },
  ],
  explanations: [
    { id: "diff:9:added", text: "第九條是模型捏造的。" },
    { id: "diff:1:modified", text: "茶會從週三改到週四。" },
  ],
  model_status: "filtered",
  model_note: "有一部分說明對不到程式產生的差異編號，那些說明沒有顯示。",
};

describe("visibleExplanations", () => {
  it("drops ids the program did not emit", () => {
    expect(visibleExplanations(diff.changes, diff.explanations)).toEqual([
      { id: "diff:1:modified", text: "茶會從週三改到週四。" },
    ]);
  });
});

describe("文件差異卡片", () => {
  it("重要變更直接顯示原文，空白差異要展開才看得到", () => {
    render(<DocumentDiffCards diff={diff} />);
    expect(screen.getByText(/茶會在週三。/)).toBeTruthy();
    expect(screen.getByText(/茶會在週四。/)).toBeTruthy();
    expect(screen.getByText("茶會從週三改到週四。")).toBeTruthy();
    expect(screen.queryByText("第九條是模型捏造的。")).toBeNull();
    expect(screen.queryByText(/座位先到先坐。/)).toBeNull();
    expect(screen.getAllByTestId("document-diff-card")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "展開原文" }));
    expect(screen.getByText(/座位先到先坐。/)).toBeTruthy();
  });

  it("解析不完整時不說沒有其他差異，也不顯示對不到的說明", () => {
    render(
      <DocumentDiffCards
        diff={{
          parse_complete: false,
          readable: true,
          summary: "程式無法完整解析這份文件，不能據此說沒有其他差異。",
          warnings: ["程式無法依條號解析其中一份文件，不能據此說沒有其他差異。"],
          changes: [],
          explanations: [{ id: "diff:1:added", text: "模型說第一條是新的。" }],
        }}
      />,
    );
    expect(screen.getByRole("alert").textContent).toContain("不能據此說沒有其他差異");
    expect(screen.queryByText(/沒有找到差異/)).toBeNull();
    expect(screen.queryByText("模型說第一條是新的。")).toBeNull();
    expect(screen.queryByTestId("document-diff-card")).toBeNull();
  });
});

describe("文件比對面板", () => {
  it("把兩份 Word 交給程式比對，並只顯示回傳的說明", async () => {
    const compared = {
      ...diff,
      explanations: [],
      model_note: "",
      model_status: "not_requested",
    };
    const compareDocuments = vi.fn(async () => compared);
    const explainDocuments = vi.fn(async () => diff);
    render(
      <DocumentVersionCompare
        compareDocuments={compareDocuments}
        explainDocuments={explainDocuments}
      />,
    );
    const before = new File(["a"], "old.docx");
    const after = new File(["b"], "new.docx");
    fireEvent.change(screen.getByLabelText("上一版"), { target: { files: [before] } });
    fireEvent.change(screen.getByLabelText("這一版"), { target: { files: [after] } });
    fireEvent.click(screen.getByRole("button", { name: "比對" }));
    expect(await screen.findByText(/茶會在週三。/)).toBeTruthy();
    expect(screen.queryByText("茶會從週三改到週四。")).toBeNull();
    expect(compareDocuments).toHaveBeenCalledWith(before, after);

    fireEvent.click(screen.getByRole("button", { name: "請模型說明" }));
    expect(explainDocuments).toHaveBeenCalledWith(before, after);
    expect(await screen.findByText("茶會從週三改到週四。")).toBeTruthy();
    expect(screen.queryByText("第九條是模型捏造的。")).toBeNull();
  });

  it("拒絕不是 docx 的檔案", () => {
    render(<DocumentVersionCompare compareDocuments={vi.fn()} explainDocuments={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("上一版"), {
      target: { files: [new File(["a"], "notes.txt")] },
    });
    expect(screen.getByRole("alert").textContent).toContain("Word");
    expect(screen.getByRole("button", { name: "比對" }).disabled).toBe(true);
  });
});

describe("平台入口裡的文件比對", () => {
  it("從導覽打開比對面板", () => {
    render(<ShellNav user={{ role: "user" }} />);
    // 文件比對在平台入口下方的「增益集」。
    fireEvent.click(screen.getByRole("button", { name: /^文件比對/ }));
    expect(screen.getByLabelText("上一版")).toBeTruthy();
    expect(screen.getByRole("dialog", { name: "文件比對" })).toBeTruthy();
  });
});
