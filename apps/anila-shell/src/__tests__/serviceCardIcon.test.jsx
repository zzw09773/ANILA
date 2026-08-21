// ServiceCard icon rendering (homepage picker → visible glyph)
//
// 治理中心選的 icon 以前寫進列就沒下文。這組斷言釘的是「畫面上真的有那個
// 圖」,不是「資料裡有 icon 欄位」。拿掉 ServiceCard 裡 <ServiceIcon /> 那一行
// 必須讓第一條變紅。
//
// IconMessage path anchor is `H7l-4 4V5` — unique vs IconImage (`M21 15l-5-5L5 21`).

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ServicesPanel } from "../services.jsx";

afterEach(() => {
  cleanup();
});

function renderPanel(icon, name) {
  const request = vi.fn().mockResolvedValue([
    {
      id: 1,
      name,
      description: "probe",
      url: "https://example.invalid",
      icon,
      launch_mode: "new_tab",
    },
  ]);
  return render(<ServicesPanel open onClose={() => {}} request={request} />);
}

describe("ServiceCard — 選了的圖示真的畫在卡片上", () => {
  it("paints the chosen icon SVG on the service card", async () => {
    renderPanel("chat", "GitLab");
    expect(await screen.findByText("GitLab")).toBeTruthy();

    const slot = document.querySelector("[data-service-icon='chat']");
    expect(slot).toBeTruthy();
    const path = slot.querySelector("svg path");
    expect(path).toBeTruthy();
    expect(path.getAttribute("d")).toContain("H7l-4 4V5");
  });

  it("unknown icon keys still render the card with the fallback glyph", async () => {
    renderPanel("definitely-not-a-real-icon", "未知圖示服務");
    expect(await screen.findByText("未知圖示服務")).toBeTruthy();

    const slot = document.querySelector(
      "[data-service-icon='definitely-not-a-real-icon']",
    );
    expect(slot).toBeTruthy();
    const path = slot.querySelector("svg path");
    expect(path).toBeTruthy();
    expect(path.getAttribute("d")).toContain("M12 3v4");
  });

  it("null or empty icon still renders the card", async () => {
    renderPanel("", "空白圖示服務");
    expect(await screen.findByText("空白圖示服務")).toBeTruthy();
    const slot = document.querySelector("[data-service-icon='']");
    expect(slot).toBeTruthy();
    expect(slot.querySelector("svg")).toBeTruthy();
  });

  // Prototype-chain names are truthy under `map[key] || fallback` and make React
  // throw ("Element type is invalid"). Lookups must never throw.
  it.each(["constructor", "valueOf", "__proto__", "hasOwnProperty", "definitely-not-a-real-icon", "", null])(
    "hostile or empty icon %p still renders the fallback glyph without throwing",
    async (icon) => {
      renderPanel(icon, "敵對鍵服務");
      expect(await screen.findByText("敵對鍵服務")).toBeTruthy();

      const attr = icon == null ? "" : String(icon);
      const slot = document.querySelector(`[data-service-icon="${attr}"]`);
      expect(slot).toBeTruthy();
      const path = slot.querySelector("svg path");
      expect(path).toBeTruthy();
      expect(path.getAttribute("d")).toContain("M12 3v4");
    },
  );
});
