// 桌面側欄右緣可拖曳／鍵盤調整寬度。寬度只活在元件狀態，窄視窗與收合不出現手把。
// @source-text-guard（最後一條讀 index.html 比對 resizer 的 CSS）
import React from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Sidebar } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { DEFAULT_FOLDERS } from "../data.jsx";
import {
  clampSidebarWidth,
  sidebarWidthBounds,
} from "../runtime/sidebarWidth.js";

const noop = () => {};
const props = {
  conversations: [],
  selectedConvId: null,
  onSelectConv: noop,
  onNewChat: noop,
  onHome: noop,
  agents: [],
  onOpenServices: noop,
  onTaskCenter: noop,
  user: { id: 1, username: "tester", role: "user" },
  onLogout: noop,
  onOpenSettings: noop,
  collapsed: false,
  onToggleCollapsed: noop,
  folder: "all",
  setFolder: noop,
  folders: DEFAULT_FOLDERS,
};

function renderSidebar(extra = {}) {
  return render(
    <ConfirmProvider>
      <Sidebar {...props} {...extra} />
    </ConfirmProvider>,
  );
}

function columnOf(separator) {
  return separator.parentElement;
}

if (typeof window.PointerEvent !== "function") {
  window.PointerEvent = class PointerEvent extends window.MouseEvent {
    constructor(type, init = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 0;
    }
  };
}

function pointer(type, init = {}) {
  const event = new window.PointerEvent(type, { bubbles: true, cancelable: true, ...init });
  Object.defineProperty(event, "pointerId", { value: init.pointerId ?? 0 });
  return event;
}

let viewport = 1280;
function setViewport(width) {
  viewport = width;
  act(() => {
    window.dispatchEvent(new Event("resize"));
  });
}

beforeEach(() => {
  if (typeof Element.prototype.setPointerCapture !== "function") {
    Element.prototype.setPointerCapture = function setPointerCapture() {};
  }
});

afterEach(() => {
  cleanup();
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  viewport = 1280;
});

describe("sidebarWidth bounds", () => {
  it("defaults to 272 and clamps to 220–420 while leaving the center at least 360", () => {
    expect(clampSidebarWidth(undefined, 1400)).toBe(272);
    expect(clampSidebarWidth(100, 1400)).toBe(220);
    expect(clampSidebarWidth(999, 1400)).toBe(420);
    expect(sidebarWidthBounds(700)).toEqual({ min: 220, max: 340 });
    expect(clampSidebarWidth(400, 700)).toBe(340);
    expect(sidebarWidthBounds(Number.NaN)).toEqual({ min: 220, max: 420 });
    expect(clampSidebarWidth(300, Number.NaN)).toBe(300);
  });
});

describe("Sidebar resize handle", () => {
  it("exposes a vertical separator on the expanded desktop sidebar only", () => {
    setViewport(1280);
    renderSidebar({ viewportWidth: () => viewport });
    const handle = screen.getByRole("separator", { name: "調整側欄寬度" });
    expect(handle.getAttribute("aria-orientation")).toBe("vertical");
    expect(handle.tabIndex).toBe(0);
    expect(handle.getAttribute("aria-valuemin")).toBe("220");
    expect(handle.getAttribute("aria-valuemax")).toBe("420");
    expect(handle.getAttribute("aria-valuenow")).toBe("272");
    expect(handle.className).toContain("anila-sidebar-resizer");
    const cascade = document.createElement("style");
    cascade.textContent = "* { box-sizing: border-box; }";
    document.head.appendChild(cascade);
    const computed = getComputedStyle(handle);
    expect(computed.boxSizing).toBe("content-box");
    expect(computed.width).toBe("6px");
    expect(computed.paddingLeft).toBe("9px");
    expect(computed.paddingRight).toBe("9px");
    cascade.remove();
    expect(columnOf(handle).style.width).toBe("272px");
    expect(columnOf(handle).style.position).toBe("relative");
  });

  it("paints a centered 2px stripe and leaves the 6px hit box unchanged", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const base = html.match(/\.anila-sidebar-resizer\s*\{[^}]*\}/);
    expect(base?.[0]).toMatch(/width:\s*6px/);
    expect(base?.[0]).toMatch(/padding:\s*0 9px/);
    expect(base?.[0]).toMatch(/right:\s*-12px/);
    const hover = html.match(
      /\.anila-sidebar-resizer:hover,\s*\.anila-sidebar-resizer:focus-visible\s*\{[^}]*\}/,
    );
    expect(hover?.[0]).toMatch(/background-image:\s*linear-gradient\(var\(--accent\), var\(--accent\)\)/);
    expect(hover?.[0]).toMatch(/background-size:\s*2px 100%/);
    expect(hover?.[0]).toMatch(/background-position:\s*center/);
    expect(hover?.[0]).toMatch(/background-repeat:\s*no-repeat/);
    expect(hover?.[0]).not.toMatch(/background-color:\s*var\(--accent\)/);
    expect(html).toMatch(/background-color:\s*Highlight/);
    expect(html).toMatch(/outline:\s*2px solid Highlight/);
  });

  it("does not render a resizer when collapsed or when the viewport is ≤900", () => {
    setViewport(1280);
    const wide = renderSidebar({ collapsed: true, viewportWidth: () => viewport });
    expect(screen.queryByRole("separator", { name: "調整側欄寬度" })).toBeNull();
    wide.unmount();
    setViewport(800);
    renderSidebar({ collapsed: false, viewportWidth: () => viewport });
    expect(screen.queryByRole("separator", { name: "調整側欄寬度" })).toBeNull();
  });

  it("drags with the left button only and restores the document cursor", () => {
    setViewport(1280);
    renderSidebar({ viewportWidth: () => viewport });
    const handle = screen.getByRole("separator", { name: "調整側欄寬度" });
    document.body.style.cursor = "wait";
    document.body.style.userSelect = "text";
    handle.dispatchEvent(pointer("pointerdown", { button: 2, clientX: 100, pointerId: 1 }));
    handle.dispatchEvent(pointer("pointermove", { button: 2, clientX: 160, pointerId: 1 }));
    expect(columnOf(handle).style.width).toBe("272px");
    expect(document.body.style.cursor).toBe("wait");

    const down = pointer("pointerdown", { button: 0, clientX: 100, pointerId: 1 });
    handle.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");
    act(() => {
      handle.dispatchEvent(pointer("pointermove", { button: 0, clientX: 116, pointerId: 1 }));
    });
    expect(columnOf(handle).style.width).toBe("288px");
    act(() => {
      handle.dispatchEvent(pointer("pointermove", { button: 0, clientX: 140, pointerId: 9 }));
    });
    expect(columnOf(handle).style.width).toBe("288px");
    act(() => {
      handle.dispatchEvent(pointer("pointermove", { button: 0, clientX: 148, pointerId: 1 }));
    });
    expect(columnOf(handle).style.width).toBe("320px");
    expect(handle.getAttribute("aria-valuenow")).toBe("320");
    handle.dispatchEvent(pointer("pointerup", { button: 0, clientX: 148, pointerId: 1 }));
    expect(document.body.style.cursor).toBe("wait");
    expect(document.body.style.userSelect).toBe("text");
  });

  it("ignores a different pointer and ends on cancel of the active one", () => {
    setViewport(1280);
    renderSidebar({ viewportWidth: () => viewport });
    const handle = screen.getByRole("separator", { name: "調整側欄寬度" });
    document.body.style.cursor = "wait";
    handle.dispatchEvent(pointer("pointerdown", { button: 0, clientX: 100, pointerId: 4 }));
    handle.dispatchEvent(pointer("pointermove", { clientX: 140, pointerId: 9 }));
    expect(columnOf(handle).style.width).toBe("272px");
    handle.dispatchEvent(pointer("pointerup", { pointerId: 9 }));
    expect(document.body.style.cursor).toBe("col-resize");
    handle.dispatchEvent(pointer("pointercancel", { pointerId: 4 }));
    expect(document.body.style.cursor).toBe("wait");
    expect(columnOf(handle).style.width).toBe("272px");
  });

  it("tracks a move outside the handle only when pointer capture is unavailable", async () => {
    setViewport(1280);
    renderSidebar({ viewportWidth: () => viewport });
    const handle = screen.getByRole("separator", { name: "調整側欄寬度" });
    handle.setPointerCapture = () => {
      throw new Error("capture unavailable");
    };
    handle.dispatchEvent(pointer("pointerdown", { button: 0, clientX: 100, pointerId: 3 }));
    await act(async () => {
      await Promise.resolve();
      window.dispatchEvent(pointer("pointermove", { clientX: 130, pointerId: 3 }));
    });
    expect(columnOf(handle).style.width).toBe("302px");
    window.dispatchEvent(pointer("pointerup", { pointerId: 3 }));
    expect(document.body.style.cursor).toBe("");
  });

  it("restores the document cursor if the pointer is lost or the sidebar unmounts mid-drag", () => {
    setViewport(1280);
    const view = renderSidebar({ viewportWidth: () => viewport });
    const handle = screen.getByRole("separator", { name: "調整側欄寬度" });
    document.body.style.cursor = "wait";
    handle.dispatchEvent(pointer("pointerdown", { button: 0, clientX: 10, pointerId: 7 }));
    expect(document.body.style.cursor).toBe("col-resize");
    handle.dispatchEvent(pointer("lostpointercapture", { pointerId: 7 }));
    expect(document.body.style.cursor).toBe("wait");

    handle.dispatchEvent(pointer("pointerdown", { button: 0, clientX: 10, pointerId: 8 }));
    expect(document.body.style.cursor).toBe("col-resize");
    view.unmount();
    expect(document.body.style.cursor).toBe("wait");
    expect(document.body.style.userSelect).toBe("");

    const again = renderSidebar({ viewportWidth: () => viewport });
    const resumed = screen.getByRole("separator", { name: "調整側欄寬度" });
    resumed.dispatchEvent(pointer("pointerdown", { button: 0, clientX: 10, pointerId: 8 }));
    expect(document.body.style.cursor).toBe("col-resize");
    setViewport(800);
    expect(document.body.style.cursor).toBe("wait");
    expect(screen.queryByRole("separator", { name: "調整側欄寬度" })).toBeNull();
    setViewport(1280);
    again.unmount();
    expect(document.body.style.cursor).toBe("wait");
    expect(document.body.style.userSelect).toBe("");
  });

  it("adjusts width from the keyboard and resets on double click", () => {
    setViewport(1280);
    renderSidebar({ viewportWidth: () => viewport });
    const handle = screen.getByRole("separator", { name: "調整側欄寬度" });
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(handle.getAttribute("aria-valuenow")).toBe("288");
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(handle.getAttribute("aria-valuenow")).toBe("272");
    fireEvent.keyDown(handle, { key: "Home" });
    expect(handle.getAttribute("aria-valuenow")).toBe("220");
    fireEvent.keyDown(handle, { key: "End" });
    expect(handle.getAttribute("aria-valuenow")).toBe("420");
    fireEvent.doubleClick(handle);
    expect(handle.getAttribute("aria-valuenow")).toBe("272");
    expect(columnOf(handle).style.width).toBe("272px");
  });

  it("reclamps on viewport resize and restores the preferred width when room returns", () => {
    setViewport(1400);
    renderSidebar({ viewportWidth: () => viewport });
    const handle = screen.getByRole("separator", { name: "調整側欄寬度" });
    fireEvent.keyDown(handle, { key: "End" });
    expect(handle.getAttribute("aria-valuenow")).toBe("420");
    const column = columnOf(handle);
    setViewport(700);
    expect(column.style.width).toBe("340px");
    setViewport(1400);
    expect(screen.getByRole("separator", { name: "調整側欄寬度" }).getAttribute("aria-valuenow")).toBe("420");
  });
});
