import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { AppShell, Sidebar, Topbar } from "../components/AppShell.jsx";

describe("AppShell", () => {
  it("渲染 overlay / sidebar / 主欄內容三個 slot", () => {
    render(
      <AppShell
        overlay={<div data-testid="overlay" />}
        sidebar={<Sidebar>側欄內容</Sidebar>}
      >
        主欄內容
      </AppShell>,
    );
    expect(screen.getByTestId("overlay")).toBeInTheDocument();
    expect(screen.getByText("側欄內容")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveTextContent("主欄內容");
  });
});

describe("Sidebar", () => {
  it("預設渲染 272px 寬欄（complementary landmark）", () => {
    render(<Sidebar aria-label="任務側欄">內容</Sidebar>);
    const aside = screen.getByRole("complementary", { name: "任務側欄" });
    expect(aside.style.width).toBe("272px");
  });

  it("collapsed 時縮成 icon rail 寬度", () => {
    render(<Sidebar collapsed>rail</Sidebar>);
    const aside = screen.getByRole("complementary");
    expect(aside.style.width).toBe("52px");
  });
});

describe("Topbar", () => {
  it("渲染頂欄容器與子項", () => {
    render(
      <Topbar data-testid="topbar">
        <span>標題</span>
      </Topbar>,
    );
    expect(screen.getByTestId("topbar")).toHaveTextContent("標題");
  });
});
