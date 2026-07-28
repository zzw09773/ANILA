// 失敗路徑不得覆蓋已生成的文字 —— W2-4 收尾(把缺陷類別清完)。
//
// W2-4 本體只修了 `sendMessage`(規格錨點只指那裡),但同樣的寫法還在另兩條路徑:
//
//   app.jsx  handleEditUser 的 catch  → `text: \`請求失敗：…\``
//   app.jsx  sendCompare   的 catch  → 同上
//
// 三分之一修好、三分之二留著,是最糟的狀態:使用者會覺得「有時候會保留、有時候
// 不會」,而那比一致地壞更難回報。所以這支把那兩條也釘住。
//
// 這裡測的是**元件層的契約**:只要訊息帶 `error` metadata,`MessageBubble` 就會
// 在**保留 text** 的前提下顯示錯誤橫幅。三條路徑共用同一個 bubble,所以契約成立
// 就等於三條都成立;而「app.jsx 有沒有真的改成掛 error 而不是覆蓋 text」由
// 下面的源碼守門條盯著(那是 grep 層,但它盯的是一個很好界定的字面模式)。

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import React from "react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { MessageBubble } from "../chat.jsx";

afterEach(cleanup);

const PARTIAL = "第三季營收較上季成長,主要來自";

const msg = (extra = {}) => ({
  id: "m1",
  role: "assistant",
  text: PARTIAL,
  citations: [{ id: "c1", title: "季報", section: "§1" }],
  ...extra,
});

describe("失敗時保留半成品", () => {
  it("帶 error 時,已生成的文字仍在 DOM(不是被錯誤訊息取代)", () => {
    render(
      <MessageBubble
        msg={msg({ streaming: false, error: { code: "UPSTREAM_TIMEOUT", message: "上游逾時" } })}
        agents={[]}
      />,
    );
    expect(screen.getByText(new RegExp(PARTIAL))).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("上游逾時");
    // 舊行為的字面必須消失 —— 它是「覆蓋」的指紋
    expect(screen.queryByText(/^請求失敗/)).toBeNull();
  });

  it("唯讀視圖(compare)不傳 onRetry:橫幅仍顯示,只是沒有重試鈕", () => {
    render(
      <MessageBubble
        msg={msg({ streaming: false, error: { code: "SERVICE_UNAVAILABLE", message: "服務暫時不可用" } })}
        agents={[]}
        onRegenerate={() => {}}
      />,
    );
    // 這條很重要:如果橫幅依賴 onRetry 才渲染,compare 視圖就會變成
    // 「答案被截斷但完全沒有失敗提示」—— 那比覆蓋文字更糟。
    expect(screen.getByRole("alert").textContent).toContain("服務暫時不可用");
    expect(screen.queryByText(/重試/)).toBeNull();
  });

  it("有 onRetry 時重試鈕出現且回傳該則訊息", () => {
    const onRetry = vi.fn();
    const m = msg({ streaming: false, error: { code: "UPSTREAM_TIMEOUT", message: "上游逾時" } });
    render(<MessageBubble msg={m} agents={[]} onRetry={onRetry} />);
    fireEvent.click(screen.getByText(/重試/));
    expect(onRetry).toHaveBeenCalledWith(m);
  });

  it("沒有 error 時不渲染橫幅", () => {
    render(<MessageBubble msg={msg({ streaming: false })} agents={[]} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("源碼守門:app.jsx 不得再用 text 覆蓋錯誤", () => {
  it("`text: `請求失敗` 這個字面在 app.jsx 已歸零", () => {
    // jsdom 的全域 `URL` 會把相對路徑解到 document base(http://localhost:3000),
    // 不是 import.meta.url —— 所以先轉成檔案路徑再用 path 往上走。
    // (repo 裡 honestWording.test.js 已經記著這個陷阱。)
    const hereDir = path.dirname(fileURLToPath(import.meta.url));
    const appPath = path.resolve(hereDir, "..", "app.jsx");
    const src = readFileSync(appPath, "utf8");
    // 只抓「把錯誤寫進 text」的形狀。`setRuntimeError(... "交接請求失敗")` 這種
    // 把錯誤放進**橫幅**的用法是對的,不該被擋 —— 所以樣式綁 `text:` 前綴。
    const overwrites = src
      .split("\n")
      .map((line, i) => [i + 1, line])
      .filter(([, line]) => /text:\s*`請求失敗/.test(line));
    expect(overwrites).toEqual([]);
  });
});
