import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Divider, Kbd } from "../components/Misc.jsx";
import { Icon } from "../icons.jsx";

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

describe("Icon", () => {
  it("轉送 SVG 屬性與自訂樣式", () => {
    render(
      <Icon data-testid="icon" aria-label="資訊" style={{ color: "red" }}>
        <path d="M1 1h1" />
      </Icon>,
    );
    const icon = screen.getByTestId("icon");
    expect(icon).toHaveAttribute("aria-label", "資訊");
    expect(icon).not.toHaveAttribute("aria-hidden");
    expect(icon.style.color).toBe("red");
  });
});
