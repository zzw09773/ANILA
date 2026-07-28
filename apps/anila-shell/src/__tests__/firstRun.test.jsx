// 首次登入導引卡(W1-10 ①)。
//
// 缺陷:`anila-changelog-seen` / `anila-dismissed-banners` 兩個旗標早就在,
// 但**沒有 first-login 旗標**,新使用者第一次看到的是一個空白對話框 + 一張
// 要打 LLM 才有內容的起始卡(air-gapped 下慢且不穩)。
//
// 這裡釘死:① 首登渲染、二登不渲染;② 導引卡是**純靜態**的(不呼叫任何
// 網路/LLM);③ 三步驟文案齊備。

import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import {
  FIRST_RUN_STORAGE_KEY,
  FIRST_RUN_STEPS,
  FirstRunGuide,
  markFirstRunSeen,
  shouldShowFirstRun,
} from "../firstRun.jsx";

beforeEach(() => {
  localStorage.clear();
});

describe("shouldShowFirstRun", () => {
  it("is true before the flag is written", () => {
    expect(shouldShowFirstRun()).toBe(true);
  });

  it("is false after markFirstRunSeen()", () => {
    markFirstRunSeen();
    expect(localStorage.getItem(FIRST_RUN_STORAGE_KEY)).toBeTruthy();
    expect(shouldShowFirstRun()).toBe(false);
  });

  it("does not throw when localStorage is unavailable", () => {
    const original = localStorage.getItem;
    localStorage.getItem = () => {
      throw new Error("SecurityError");
    };
    try {
      expect(shouldShowFirstRun()).toBe(false);
    } finally {
      localStorage.getItem = original;
    }
  });
});

describe("FirstRunGuide", () => {
  it("renders on first login", () => {
    render(<FirstRunGuide />);
    expect(screen.getByRole("region", { name: /第一次使用/ })).toBeInTheDocument();
    for (const step of FIRST_RUN_STEPS) {
      expect(screen.getByText(step.title)).toBeInTheDocument();
    }
  });

  it("does not render on the second login", () => {
    markFirstRunSeen();
    const { container } = render(<FirstRunGuide />);
    expect(container).toBeEmptyDOMElement();
  });

  it("is purely static — renders without touching the network", () => {
    const fetchSpy = vi.fn();
    const original = globalThis.fetch;
    globalThis.fetch = fetchSpy;
    try {
      render(<FirstRunGuide />);
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      globalThis.fetch = original;
    }
  });

  it("dismiss writes the flag so it never comes back", () => {
    const { container } = render(<FirstRunGuide />);
    fireEvent.click(screen.getByRole("button", { name: /知道了/ }));
    expect(shouldShowFirstRun()).toBe(false);
    expect(container).toBeEmptyDOMElement();
  });

  it("names exactly the three steps the remediation plan asked for", () => {
    expect(FIRST_RUN_STEPS).toHaveLength(3);
    const titles = FIRST_RUN_STEPS.map((s) => s.title).join(" | ");
    expect(titles).toMatch(/agent/i);
    expect(titles).toMatch(/知識庫/);
    expect(titles).toMatch(/提問|問/);
  });
});
