import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Table } from "../components/Table.jsx";

const COLUMNS = [
  { key: "name", title: "名稱" },
  { key: "trace", title: "Trace ID", mono: true },
];

describe("Table", () => {
  it("渲染表頭與資料列", () => {
    render(
      <Table
        columns={COLUMNS}
        rows={[{ name: "文件問答", trace: "tr_123" }]}
        rowKey={(r) => r.trace}
      />,
    );
    expect(screen.getByRole("columnheader", { name: "名稱" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "文件問答" })).toBeInTheDocument();
  });

  it("mono 欄位使用等寬字 token（doc 12：等寬保留給資料）", () => {
    render(
      <Table columns={COLUMNS} rows={[{ name: "文件問答", trace: "tr_123" }]} />,
    );
    const cell = screen.getByRole("cell", { name: "tr_123" });
    expect(cell.style.fontFamily).toContain("--anila-font-mono");
  });

  it("空資料顯示繁中空狀態", () => {
    render(<Table columns={COLUMNS} rows={[]} />);
    expect(screen.getByText("目前沒有資料")).toBeInTheDocument();
  });
});
