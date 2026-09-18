// 窄視窗版面（UI 評估 2026-09-02 第 1 件）：400px 寬時側欄不收，主欄被擠成直排字。
// 期望：視窗寬度 ≤ 900px 時側欄一開始就是收合的（工具列上是「展開側邊」）；
// 寬視窗維持展開（「收合側邊」）。使用者手動切換仍然有效。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { mountOrchestrator } from "./helpers/orchestrator.jsx";

function stubMatchMedia(matchesNarrow) {
  const listeners = new Set();
  const mql = {
    matches: matchesNarrow,
    media: "(max-width: 900px)",
    addEventListener: (_t, fn) => listeners.add(fn),
    removeEventListener: (_t, fn) => listeners.delete(fn),
    addListener: (fn) => listeners.add(fn),
    removeListener: (fn) => listeners.delete(fn),
    dispatchEvent: () => false,
    onchange: null,
    _fire(matches) { mql.matches = matches; listeners.forEach((fn) => fn({ matches })); },
  };
  vi.stubGlobal("matchMedia", vi.fn(() => mql));
  return mql;
}

let backend;
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("窄視窗時側欄自動收合", () => {
  it("≤900px：一開始就是收合（工具列顯示「展開側邊」）", async () => {
    stubMatchMedia(true);
    await mountOrchestrator();
    expect(screen.getAllByTitle("展開側邊").length).toBeGreaterThan(0);
    expect(screen.queryByTitle("收合側邊")).toBeNull();
  });

  it("寬視窗：維持展開（「收合側邊」）", async () => {
    stubMatchMedia(false);
    await mountOrchestrator();
    expect(screen.getByTitle("收合側邊")).toBeTruthy();
  });

  it("寬→窄的視窗變化會收合，窄→寬會展開", async () => {
    const mql = stubMatchMedia(false);
    await mountOrchestrator();
    expect(screen.getByTitle("收合側邊")).toBeTruthy();
    const { act } = await import("@testing-library/react");
    await act(async () => { mql._fire(true); });
    expect(screen.getAllByTitle("展開側邊").length).toBeGreaterThan(0);
    await act(async () => { mql._fire(false); });
    expect(screen.getByTitle("收合側邊")).toBeTruthy();
  });
});
