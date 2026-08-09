import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render as renderView, screen, waitFor } from "@testing-library/react";

const mermaidMock = vi.hoisted(() => ({
  initialize: vi.fn(),
  render: vi.fn(),
}));

vi.mock("mermaid", () => ({ default: mermaidMock }));

import { MarkdownView, mermaidThemeForApp } from "../markdown.jsx";

const DIAGRAM = "```mermaid\ngraph TD\n  A --> B\n```";

afterEach(() => {
  cleanup();
  document.querySelectorAll('[id^="dmmd-"]').forEach((node) => node.remove());
});

beforeEach(() => {
  mermaidMock.initialize.mockClear();
  mermaidMock.render.mockReset();
  document.documentElement.removeAttribute("data-theme");
});

describe("MermaidDiagram theme and failed-render wiring", () => {
  it("maps dark, light, and unset app themes to Mermaid themes", () => {
    expect(mermaidThemeForApp("dark")).toBe("dark");
    expect(mermaidThemeForApp("light")).toBe("default");
    expect(mermaidThemeForApp()).toBe("default");
  });

  it("passes the app dark theme and removes Mermaid's injected error node", async () => {
    mermaidMock.render.mockImplementation(async (renderId) => {
      const stray = document.createElement("svg");
      stray.id = `d${renderId}`;
      const sibling = document.createElement("svg");
      sibling.id = `${stray.id}-sibling`;
      document.body.appendChild(stray);
      document.body.appendChild(sibling);
      throw new Error("syntax error");
    });
    document.documentElement.setAttribute("data-theme", "dark");

    renderView(<MarkdownView text={DIAGRAM} />);

    await screen.findByText(/mermaid 錯誤：syntax error/);
    expect(mermaidMock.initialize).toHaveBeenCalledWith(expect.objectContaining({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "dark",
    }));
    const renderId = mermaidMock.render.mock.calls[0][0];
    expect(document.getElementById(`d${renderId}`)).toBeNull();
    expect(document.getElementById(`d${renderId}-sibling`)).not.toBeNull();
  });

  it("re-renders an existing diagram when the app theme attribute changes", async () => {
    mermaidMock.render.mockResolvedValue({ svg: "<svg data-rendered=\"true\"></svg>" });
    document.documentElement.setAttribute("data-theme", "light");

    renderView(<MarkdownView text={DIAGRAM} />);
    await waitFor(() => expect(mermaidMock.render).toHaveBeenCalledTimes(1));
    expect(mermaidMock.initialize.mock.calls[0][0]).toEqual(expect.objectContaining({
      securityLevel: "strict",
      theme: "default",
    }));

    document.documentElement.setAttribute("data-theme", "dark");
    await waitFor(() => expect(mermaidMock.render).toHaveBeenCalledTimes(2));

    expect(mermaidMock.initialize.mock.calls.at(-1)[0]).toEqual(expect.objectContaining({
      securityLevel: "strict",
      theme: "dark",
    }));
  });
});
