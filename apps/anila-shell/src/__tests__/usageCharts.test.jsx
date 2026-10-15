import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor, act } from "@testing-library/react";
import UsageCharts from "../components/UsageCharts.jsx";
import { UsagePage } from "../usage.jsx";
import { usageBodyTokens, usageCalendarDays, usageLinePoints } from "../runtime/usageDisplay.js";

const now = Date.parse("2026-10-15T04:00:00Z");
const rows = [
  { date: "2026-10-14", prompt_tokens: 10, completion_tokens: 20, reasoning_tokens: 500 },
  { date: "2026-10-15", prompt_tokens: 40, completion_tokens: 60, reasoning_tokens: 900 },
  { date: "2025-01-01", prompt_tokens: 99999 },
];
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("用量時間序列", () => {
  it("沿用 API 正文總數，by_day 無 total 時以輸入加輸出計算且不含思考", () => {
    expect(usageBodyTokens({ prompt_tokens: 10, completion_tokens: 20, reasoning_tokens: 90, total_tokens: 30 })).toBe(30);
    expect(usageBodyTokens({ prompt_tokens: 10, completion_tokens: 20, reasoning_tokens: 90 })).toBe(30);
    expect(usageBodyTokens({ total_tokens: 30 })).toBe(30);
  });
  it("台北日界、rolling 部分日與範圍外資料", () => {
    const days = usageCalendarDays(rows, "24h", now);
    expect(days).toEqual([
      { date: "2026-10-14", total: 30, reasoning: 500, partial: true },
      { date: "2026-10-15", total: 100, reasoning: 900, partial: true },
    ]);
    expect(usageCalendarDays([], "7d", now)).toHaveLength(8);
    expect(usageCalendarDays([], "30d", now)).toHaveLength(31);
    expect(usageCalendarDays([], "24h", Date.parse("2026-10-15T17:00:00Z")).at(-1).date).toBe("2026-10-16");
  });
  it("僅期間內補零，週一分組與累計不含思考", () => {
    const days = usageCalendarDays(rows, "7d", now);
    expect(days[0]).toEqual({ date: "2026-10-08", total: 0, reasoning: 0, partial: true });
    expect(usageLinePoints(days, "cumulative").at(-1).total).toBe(130);
    expect(usageLinePoints(days, "weekly").map((point) => point.total)).toEqual([0, 130]);
    expect(usageLinePoints(days, "daily").slice(-2).map((point) => point.reasoning)).toEqual([500, 900]);
    expect(usageLinePoints(days, "weekly").map((point) => point.reasoning)).toEqual([0, 1400]);
    expect(usageLinePoints(days, "cumulative").slice(-2).map((point) => point.reasoning)).toEqual([500, 1400]);
  });
  it("折線切換、每日一格、hover/focus/click 提供日期與 Token", () => {
    render(<UsageCharts rows={rows} range="24h" requestedAt={now} />);
    expect(screen.getByTestId("usage-line").dataset.values).toBe("30,100");
    expect(document.querySelectorAll("[data-usage-date]")).toHaveLength(2);
    const day = screen.getByRole("button", { name: "2026-10-14：30 tokens · 思考 500（另計）（含部分日）" });
    for (const action of [fireEvent.mouseEnter, fireEvent.focus, fireEvent.click]) {
      action(day);
      expect(screen.getByRole("status").textContent).toBe(day.getAttribute("aria-label"));
    }
    expect(screen.getByLabelText("日曆色階：低到高")).toBeTruthy();
    expect(screen.getByText("10月")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "期間累計" }));
    expect(screen.getByTestId("usage-line").dataset.values).toBe("30,130");
    expect(screen.getByRole("status").textContent).toContain("130 tokens · 思考 1,400（另計）");
    expect(screen.getByText(/2026-10-15：130 tokens · 思考 1,400（另計）/, { selector: "li" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "每週" }));
    expect(screen.getByTestId("usage-line").dataset.values).toBe("130");
    expect(screen.getByText(/2026-10-14 ～ 2026-10-15：130 tokens · 思考 1,400（另計）/, { selector: "li" })).toBeTruthy();
    expect(day.title).toContain("思考 500（另計）");
  });
  it("思考零值仍有提示且不影響熱度與正文折線", () => {
    const { rerender } = render(<UsageCharts rows={rows} range="24h" requestedAt={now} />);
    const color = document.querySelector('[data-usage-date="2026-10-14"]').style.background;
    rerender(<UsageCharts rows={rows.map((row) => ({ ...row, reasoning_tokens: 0 }))} range="24h" requestedAt={now} />);
    const day = document.querySelector('[data-usage-date="2026-10-14"]');
    expect(day.title).toContain("思考 0（另計）");
    expect(day.style.background).toBe(color);
    expect(screen.getByTestId("usage-line").dataset.values).toBe("30,100");
    fireEvent.click(day);
    expect(screen.getByRole("status").textContent).toContain("思考 0（另計）");
    expect(screen.getByText(/2026-10-14：30 tokens · 思考 0（另計）/, { selector: "li" })).toBeTruthy();
  });
  it("週一起始、單一 Tab 入口與畫面方向鍵邊界、點選及範圍重設", () => {
    const { rerender } = render(<UsageCharts rows={rows} range="30d" requestedAt={now} />);
    const cells = () => [...document.querySelectorAll('[data-usage-date]')];
    const tabStops = () => cells().filter((cell) => cell.tabIndex === 0);
    const find = (date) => document.querySelector(`[data-usage-date="${date}"]`);
    expect(tabStops()).toEqual([cells()[0]]);
    const monday = find("2026-10-05");
    const sunday = find("2026-10-04");
    expect(monday.style.gridRow).toBe("2");
    expect(sunday.style.gridRow).toBe("8");
    expect(Number(monday.style.gridColumn)).toBe(Number(sunday.style.gridColumn) + 1);
    expect(screen.getByText("由上到下：週一～週日")).toBeTruthy();
    expect(screen.getByText("10月").style.gridColumn).toBe(find("2026-10-01").style.gridColumn);
    fireEvent.click(monday);
    expect(tabStops()).toEqual([monday]);
    const key = (value, expected) => {
      fireEvent.keyDown(document.activeElement, { key: value });
      expect(document.activeElement).toBe(expected);
      expect(tabStops()).toEqual([expected]);
    };
    key("ArrowUp", monday);
    key("ArrowDown", find("2026-10-06"));
    key("ArrowUp", monday);
    key("ArrowLeft", find("2026-09-28"));
    key("ArrowRight", monday);
    fireEvent.click(sunday);
    key("ArrowDown", sunday);
    key("Home", cells()[0]);
    key("ArrowUp", cells()[0]);
    key("ArrowLeft", cells()[0]);
    key("End", cells().at(-1));
    key("ArrowDown", cells().at(-1));
    key("ArrowRight", cells().at(-1));
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    document.activeElement.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(false);
    expect(document.getElementById(monday.getAttribute("aria-describedby")).textContent).toContain("Tab 離開日曆");
    rerender(<UsageCharts rows={rows} range="24h" requestedAt={now} />);
    expect(tabStops()).toEqual([cells()[0]]);
  });
  it("空資料不製造全年格子", () => {
    render(<UsageCharts rows={[]} range="30d" requestedAt={now} />);
    expect(document.querySelectorAll("[data-usage-date]")).toHaveLength(31);
    expect([...document.querySelectorAll("[data-day-total]")].every((node) => node.dataset.dayTotal === "0")).toBe(true);
  });
});

it("切範圍立即隱藏舊圖，取消的慢請求與錯誤不能覆蓋新結果", async () => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const pending = {};
  const request = vi.fn((url) => new Promise((resolve, reject) => { pending[url.split("=")[1]] = { resolve, reject }; }));
  render(<UsagePage open onClose={() => {}} request={request} />);
  const answer = (range, count) => ({ range, requests: count, prompt_tokens: count, completion_tokens: 0, by_day: [{ date: "2026-10-15", prompt_tokens: count }], by_model: [], by_kind: [] });
  await act(async () => pending["7d"].resolve(answer("7d", 7)));
  expect(screen.getByTestId("usage-line")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "近30天" }));
  expect(screen.queryByTestId("usage-line")).toBeNull();
  expect(screen.getByText("載入用量中…")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "近24小時" }));
  await act(async () => pending["24h"].resolve(answer("24h", 24)));
  await act(async () => pending["30d"].reject(new Error("過期錯誤")));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByTestId("usage-metric-requests").textContent).toContain("24");
  expect(document.querySelectorAll("[data-usage-date]")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "近7天" }));
  await act(async () => pending["7d"].resolve(answer("7d", 70)));
  await waitFor(() => expect(screen.getByTestId("usage-metric-requests").textContent).toContain("70"));
  fireEvent.click(screen.getByRole("button", { name: "近30天" }));
  fireEvent.click(screen.getByRole("button", { name: "近24小時" }));
  await act(async () => pending["24h"].resolve(answer("24h", 240)));
  await act(async () => pending["30d"].resolve(answer("30d", 999)));
  expect(screen.getByTestId("usage-metric-requests").textContent).toContain("240");
  expect(document.querySelectorAll("[data-usage-date]")).toHaveLength(2);
});
