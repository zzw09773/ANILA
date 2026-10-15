import React from "react";
import { ShellNav } from "../shellNav.jsx";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { TranslationPanel } from "../translationPanel.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { Composer } from "../chat.jsx";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => ({}) })));
});

function renderPanel(props = {}) {
  return render(<TranslationPanel open onClose={vi.fn()} {...props} />);
}

describe("翻譯雙模式", () => {
  it("快速閱讀只顯示譯文，不畫紅框", () => {
    renderPanel();
    expect(screen.getByRole("radio", { name: /快速閱讀/ })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "載入合成範例" }));
    expect(screen.getByLabelText("譯文").value).toContain("13");
    expect(screen.queryByTestId("translation-mismatch")).toBeNull();
    expect(screen.queryByTestId("translation-unsettled")).toBeNull();
    expect(screen.getByText("快速閱讀不做檢查。")).toBeInTheDocument();
  });

  it("對外使用用紅框標對不上，並標未定譯", () => {
    renderPanel();
    fireEvent.click(screen.getByRole("radio", { name: /對外使用/ }));
    fireEvent.click(screen.getByRole("button", { name: "載入合成範例" }));
    expect(screen.getByTestId("translation-example-note").textContent).toContain("合成範例");
    const boxes = screen.getAllByTestId("translation-mismatch");
    expect(boxes.length).toBeGreaterThan(0);
    expect(boxes.some((box) => box.textContent.includes("13"))).toBe(true);
    expect(screen.getAllByTestId("translation-mismatch-note").length).toBeGreaterThan(0);
    expect(screen.getByTestId("translation-unsettled").textContent).toContain("未定譯");
    expect(screen.getByTestId("translation-unsettled").textContent).toContain("calibration");
  });

  it("改譯文後紅框跟著重算", () => {
    renderPanel();
    fireEvent.click(screen.getByRole("radio", { name: /對外使用/ }));
    fireEvent.change(screen.getByLabelText("原文"), {
      target: { value: "Wait 5 days." },
    });
    fireEvent.change(screen.getByLabelText("譯文"), {
      target: { value: "等待 5 天。" },
    });
    expect(screen.getByText("數字、單位和否定詞都對得上，沒有未定譯。")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("譯文"), {
      target: { value: "等待 6 天。" },
    });
    expect(screen.getByTestId("translation-mismatch").textContent).toContain("6");
  });

  it("翻譯按鈕把原文交給現有翻譯函式，不把檢查模式交出去", async () => {
    const translateText = vi.fn(async () => "完整譯文在這裡。");
    renderPanel({ translateText });
    fireEvent.change(screen.getByLabelText("原文"), { target: { value: "Hello sensor." } });
    fireEvent.click(screen.getByRole("button", { name: "翻譯" }));
    expect(await screen.findByDisplayValue("完整譯文在這裡。")).toBeInTheDocument();
    expect(translateText).toHaveBeenCalledWith(expect.objectContaining({
      source: "Hello sensor.",
      targetLabel: "繁體中文（臺灣）",
    }));
    expect(translateText.mock.calls[0][0].mode).toBeUndefined();
  });

  it("增益集可以打開翻譯視窗，說明分清楚兩種模式；輸入列不再有翻譯鈕", () => {
    const { unmount } = render(
      <ConfirmProvider>
        <Composer onSend={vi.fn()} agents={[{ id: "anila-router", name: "ANILA", short: "auto" }]} />
      </ConfirmProvider>,
    );
    expect(screen.queryByRole("button", { name: "翻譯" })).toBeNull();
    unmount();
    render(<ShellNav user={{ role: "user" }} />);
    fireEvent.click(screen.getByRole("button", { name: /增益集/ }));
    const entry = screen.getByRole("button", { name: /^翻譯/ });
    expect(entry.textContent).toContain("快速閱讀：給自己看懂");
    expect(entry.textContent).toContain("對外使用：要寄出的譯文");
    fireEvent.click(entry);
    expect(screen.getByRole("dialog", { name: "翻譯" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /快速閱讀/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /對外使用/ })).toBeInTheDocument();
  });
});
