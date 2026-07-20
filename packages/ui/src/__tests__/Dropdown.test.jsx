import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Dropdown, MenuItem } from "../components/Dropdown.jsx";

describe("Dropdown", () => {
  it("點擊 trigger 後展開面板，ESC 收合", () => {
    render(
      <Dropdown trigger={() => <button>選單</button>}>
        <MenuItem onClick={() => {}}>項目一</MenuItem>
      </Dropdown>,
    );
    expect(screen.queryByText("項目一")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "選單" }));
    expect(screen.getByText("項目一")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("項目一")).not.toBeInTheDocument();
  });

  it("children 為函式時取得 close callback", () => {
    render(
      <Dropdown trigger={() => <button>選單</button>}>
        {(close) => <MenuItem onClick={close}>關閉我</MenuItem>}
      </Dropdown>,
    );
    fireEvent.click(screen.getByRole("button", { name: "選單" }));
    fireEvent.click(screen.getByText("關閉我"));
    expect(screen.queryByText("關閉我")).not.toBeInTheDocument();
  });
});

describe("MenuItem", () => {
  it("點擊觸發 onClick", () => {
    const onClick = vi.fn();
    render(<MenuItem onClick={onClick}>重新命名</MenuItem>);
    fireEvent.click(screen.getByRole("button", { name: "重新命名" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
