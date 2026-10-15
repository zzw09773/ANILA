import React from "react";
import { ShellNav } from "../shellNav.jsx";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { LetterRewritePanel } from "../letterRewrite.jsx";
import { requestLetterRewrite } from "../runtime/letterRewriteRequest.js";

vi.mock("../runtime/letterRewriteRequest.js", async () => {
  const actual = await vi.importActual("../runtime/letterRewriteRequest.js");
  return {
    ...actual,
    requestLetterRewrite: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function renderPanel() {
  return render(
    <LetterRewritePanel open onClose={() => {}} />,
  );
}

describe("改寫信件面板", () => {
  it("三個按鈕用指定的名稱", () => {
    renderPanel();
    expect(screen.getByRole("button", { name: "更正式" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "更和緩" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "更精簡" })).toBeTruthy();
  });

  it("沒有文字時不送出", () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "更正式" }));
    expect(requestLetterRewrite).not.toHaveBeenCalled();
  });

  it("並排顯示原文和改寫，改動的日期標紅", async () => {
    requestLetterRewrite.mockResolvedValue("場地改於3月9日借用，費用新臺幣1,200元，請於5個工作天內回覆。");
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "填入範例" }));
    fireEvent.click(screen.getByRole("button", { name: "更和緩" }));

    await waitFor(() => expect(screen.getByTestId("letter-compare")).toBeTruthy());
    expect(requestLetterRewrite).toHaveBeenCalledWith(expect.objectContaining({
      tone: "softer",
      original: expect.stringContaining("3月2日"),
    }));
    expect(screen.getByLabelText("原文").textContent).toContain("3月2日");
    expect(screen.getByLabelText("原文").textContent).toContain("1,200元");
    const column = screen.getByTestId("letter-rewrite-column");
    const dateMark = column.querySelector("[data-letter-fact='date']");
    expect(dateMark.textContent).toBe("3月9日");
    expect(dateMark.getAttribute("data-letter-changed")).toBe("1");
    expect(dateMark.style.color).toBe("var(--danger)");
    expect(column.querySelector("[data-letter-fact='amount']")).toBeNull();
    expect(column.querySelector("[data-letter-fact='deadline']")).toBeNull();
    expect(screen.getByText(/紅字由程式比對原文而來/)).toBeTruthy();
  });

  it("改寫失敗時顯示繁體中文說明", async () => {
    requestLetterRewrite.mockRejectedValue(new Error("這次改寫沒有完成。請再試一次。"));
    renderPanel();
    fireEvent.change(screen.getByLabelText("信件或公文原文"), { target: { value: "請於3月2日回覆。" } });
    fireEvent.click(screen.getByRole("button", { name: "更精簡" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("這次改寫沒有完成。請再試一次。");
    expect(screen.queryByTestId("letter-compare")).toBeNull();
  });
});

describe("輸入框可以打開改寫", () => {
  it("從增益集打開並排改寫，輸入框工具列不再有這顆", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false })));
    requestLetterRewrite.mockResolvedValue("敬請於3月2日前回覆，費用新臺幣1,200元。");
    const { unmount } = render(
      <ConfirmProvider>
        <Composer onSend={vi.fn()} agents={[{ id: "anila-router", name: "ANILA", short: "auto" }]} />
      </ConfirmProvider>,
    );
    expect(screen.queryByRole("button", { name: "改寫信件與公文" })).toBeNull();
    unmount();
    window.localStorage.clear();
    render(<ShellNav user={{ role: "user" }} />);
    fireEvent.click(screen.getByRole("button", { name: /增益集/ }));
    fireEvent.click(screen.getByRole("button", { name: /^改寫信件與公文/ }));
    fireEvent.change(screen.getByLabelText("信件或公文原文"), {
      target: { value: "3月2日前回覆，費用新臺幣1,200元。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "更正式" }));
    await waitFor(() => expect(screen.getByTestId("letter-compare")).toBeTruthy());
    expect(screen.getByLabelText("改寫").textContent).toContain("敬請於3月2日前回覆");
    expect(screen.getByTestId("letter-rewrite-column").querySelector("[data-letter-changed]")).toBeNull();
  });
});
