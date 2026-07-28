// W2-9 風險釘子 —— 傳給 memo 化元件的 callback 必須引用穩定。
//
// `React.memo` 用的是預設淺比較,所以**任何**一個 prop 的 identity 變了就整個
// 白做。`app.jsx` 的 handler 全是元件本體內的 function 宣告(每次 render 都是
// 新的),`onPickFollowUp` 更是 inline closure —— 不先把它們釘穩,memo 化只會
// 讓 bundle 變大而不會少一次 render。
//
// `useCallback` 在這裡救不了:這些 handler 讀 `messagesByConv`,而它在串流期間
// **每個 token 都在變** —— 把它放進 deps 等於沒有 memo。所以走 latest-ref
// 模式:對外的函式身分固定,內部永遠呼叫最新那份閉包。

import { describe, it, expect, vi } from "vitest";
import React, { useState } from "react";
import { render, renderHook, act, fireEvent, screen } from "@testing-library/react";
import { useStableCallback } from "../runtime/useStableCallback.js";

describe("useStableCallback", () => {
  it("跨 re-render 身分不變", () => {
    const { result, rerender } = renderHook(({ fn }) => useStableCallback(fn), {
      initialProps: { fn: () => "a" },
    });
    const first = result.current;
    rerender({ fn: () => "b" });
    rerender({ fn: () => "c" });
    expect(result.current).toBe(first);
  });

  it("呼叫時跑的是**最新**那份閉包(不是 mount 時那份)", () => {
    const { result, rerender } = renderHook(({ value }) => {
      const cb = useStableCallback(() => value);
      return cb;
    }, { initialProps: { value: 1 } });

    expect(result.current()).toBe(1);
    rerender({ value: 2 });
    expect(result.current()).toBe(2);
    rerender({ value: 3 });
    expect(result.current()).toBe(3);
  });

  it("引數與回傳值原樣透傳", () => {
    const spy = vi.fn((a, b) => a + b);
    const { result } = renderHook(() => useStableCallback(spy));
    expect(result.current(2, 3)).toBe(5);
    expect(spy).toHaveBeenCalledWith(2, 3);
  });

  it("undefined / null 的 handler 呼叫時不炸", () => {
    const { result } = renderHook(() => useStableCallback(undefined));
    expect(() => result.current("x")).not.toThrow();
    expect(result.current("x")).toBeUndefined();
  });

  it("實戰:state 每次都變,但 handler 讀得到最新 state 且身分固定", () => {
    const identities = new Set();
    let seen = null;

    function Widget() {
      const [n, setN] = useState(0);
      const report = useStableCallback(() => { seen = n; });
      identities.add(report);
      return (
        <>
          <button onClick={() => setN((v) => v + 1)}>bump</button>
          <button onClick={report}>report</button>
          <span data-testid="n">{n}</span>
        </>
      );
    }

    render(<Widget />);
    act(() => { fireEvent.click(screen.getByText("bump")); });
    act(() => { fireEvent.click(screen.getByText("bump")); });
    expect(screen.getByTestId("n").textContent).toBe("2");

    act(() => { fireEvent.click(screen.getByText("report")); });
    expect(seen).toBe(2);
    expect(identities.size).toBe(1);
  });
});
