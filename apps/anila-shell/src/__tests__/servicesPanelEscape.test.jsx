// 逐頁走查 2026-09-02：專案入口抽屜開著時按 Escape 沒反應（設定、分享、附件檢視都會關）。
// 一個 aria-modal 對話框不吃 Escape，鍵盤使用者只能用滑鼠找右上角的叉。
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ServicesPanel } from "../services.jsx";

describe("專案入口抽屜的 Escape", () => {
  it("開著時按 Escape 會呼叫 onClose", async () => {
    const request = vi.fn().mockResolvedValue({ services: [] });
    const onClose = vi.fn();
    render(<ServicesPanel open onClose={onClose} request={request} />);
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("關著時 Escape 不會亂呼叫 onClose", () => {
    const onClose = vi.fn();
    render(<ServicesPanel open={false} onClose={onClose} request={vi.fn()} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});
