import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Divider, Kbd } from "../components/Misc.jsx";

describe("Kbd", () => {
  it("以等寬字 token 渲染快捷鍵", () => {
    render(<Kbd>⌘K</Kbd>);
    const kbd = screen.getByText("⌘K");
    expect(kbd.style.fontFamily).toContain("--anila-font-mono");
  });
});

describe("Divider", () => {
  it("水平分隔線佔滿寬度", () => {
    const { container } = render(<Divider />);
    expect(container.firstChild.style.height).toBe("1px");
    expect(container.firstChild.style.width).toBe("100%");
  });

  it("vertical 時寬 1px", () => {
    const { container } = render(<Divider vertical />);
    expect(container.firstChild.style.width).toBe("1px");
  });
});
