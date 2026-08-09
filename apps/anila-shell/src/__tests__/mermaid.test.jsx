import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render as renderView, screen, waitFor } from "@testing-library/react";

const mermaidMock = vi.hoisted(() => ({
  initialize: vi.fn(),
  render: vi.fn(),
}));

vi.mock("mermaid", () => ({ default: mermaidMock }));

import { MarkdownView } from "../markdown.jsx";

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
  it("passes the app dark theme and removes Mermaid's injected error node", async () => {
    mermaidMock.render.mockImplementation(async (renderId) => {
      const stray = document.createElement("svg");
      stray.id = `d${renderId}`;
      document.body.appendChild(stray);
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
    expect(document.querySelectorAll('[id^="dmmd-"]')).toHaveLength(0);
  });

  it("re-renders an existing diagram when the app theme attribute changes", async () => {
    mermaidMock.render.mockResolvedValue({ svg: "<svg data-rendered=\"true\"></svg>" });
    document.documentElement.setAttribute("data-theme", "light");

    renderView(<MarkdownView text={DIAGRAM} />);
    await waitFor(() => expect(mermaidMock.render).toHaveBeenCalledTimes(1));

    document.documentElement.setAttribute("data-theme", "dark");
    await waitFor(() => expect(mermaidMock.render).toHaveBeenCalledTimes(2));

    expect(mermaidMock.initialize.mock.calls.at(-1)[0]).toEqual(expect.objectContaining({
      securityLevel: "strict",
      theme: "dark",
    }));
  });
});
