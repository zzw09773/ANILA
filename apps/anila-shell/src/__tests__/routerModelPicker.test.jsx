import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import RouterModelPicker from "../components/RouterModelPicker.jsx";

const MODELS = [
  { id: 3, name: "glm-example", display_name: "GLM", health_status: "healthy" },
  { id: 4, name: "qwen-example", display_name: "Qwen", health_status: "degraded" },
];

describe("RouterModelPicker", () => {
  it("lists granted models and keeps anila-router out of the menu", () => {
    render(
      <RouterModelPicker
        models={MODELS}
        selectedId={3}
        defaultModelId={3}
        onChange={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText("此則對話使用的模型，僅由 ANILA 自動派工時可選"));
    expect(screen.getAllByText("GLM").length).toBeGreaterThan(0);
    expect(screen.getByText("全院預設")).toBeTruthy();
    expect(screen.getByText("Qwen")).toBeTruthy();
    expect(screen.getByText("不穩")).toBeTruthy();
    expect(screen.queryByText(/anila-router/)).toBeNull();
    expect(screen.queryByText("ANILA")).toBeNull();
  });

  it("saves a new selection", () => {
    const onChange = vi.fn();
    render(
      <RouterModelPicker models={MODELS} selectedId={3} defaultModelId={3} onChange={onChange} />,
    );
    fireEvent.click(screen.getByLabelText("此則對話使用的模型，僅由 ANILA 自動派工時可選"));
    fireEvent.click(screen.getByText("Qwen"));
    expect(onChange).toHaveBeenCalledWith(4);
  });

  it("keeps the conversation model name when the list has not loaded", () => {
    render(
      <RouterModelPicker
        models={[]}
        selectedId={5}
        fallbackName="Qwen 3.8 Flash"
        onChange={() => {}}
      />,
    );
    const trigger = screen.getByLabelText("此則對話使用的模型，僅由 ANILA 自動派工時可選");
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent("Qwen 3.8 Flash");
    expect(screen.queryByText("沒有可用模型")).toBeNull();
  });

  it("不列出已停用、未授權、已關閉選單或平台入口", () => {
    render(
      <RouterModelPicker
        models={[
          ...MODELS,
          {
            id: 9,
            name: "glm-5.3-flash",
            display_name: "glm-5.3-flash",
            is_active: false,
            router_enabled: true,
            grant_sources: ["all"],
          },
          {
            id: 10,
            name: "secret",
            display_name: "秘密模型",
            is_active: true,
            router_enabled: true,
            grant_sources: [],
          },
          {
            id: 11,
            name: "closed-menu",
            display_name: "已關閉選單",
            is_active: true,
            router_enabled: false,
            grant_sources: ["all"],
          },
          {
            id: 12,
            name: "anila-router",
            display_name: "ANILA",
            is_active: true,
            router_enabled: true,
            grant_sources: ["all"],
          },
        ]}
        selectedId={3}
        defaultModelId={3}
        onChange={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText("此則對話使用的模型，僅由 ANILA 自動派工時可選"));
    expect(screen.getByText("Qwen")).toBeTruthy();
    expect(screen.queryByText("glm-5.3-flash")).toBeNull();
    expect(screen.queryByText("秘密模型")).toBeNull();
    expect(screen.queryByText("已關閉選單")).toBeNull();
    expect(screen.queryByText("ANILA")).toBeNull();
    expect(screen.queryByText("anila-router")).toBeNull();
  });

  it("非預設模型顯示沿用上次選擇，回到預設會選回全院預設", () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <RouterModelPicker models={MODELS} selectedId={4} defaultModelId={3} onChange={onChange} />,
    );
    expect(screen.getByText("沿用上次選擇")).toBeTruthy();
    const back = screen.getByRole("button", { name: "回到預設" });
    expect(back.getAttribute("title")).toBe("切換到全院預設模型");
    fireEvent.click(back);
    expect(onChange).toHaveBeenCalledWith(3);
    rerender(
      <RouterModelPicker models={MODELS} selectedId={3} defaultModelId={3} onChange={onChange} />,
    );
    expect(screen.queryByText("沿用上次選擇")).toBeNull();
    expect(screen.queryByRole("button", { name: "回到預設" })).toBeNull();
  });

  it("送出中回到預設也鎖住，不會改模型", () => {
    const onChange = vi.fn();
    render(
      <RouterModelPicker models={MODELS} selectedId={4} defaultModelId={3} disabled onChange={onChange} />,
    );
    const back = screen.getByRole("button", { name: "回到預設" });
    expect(back.disabled).toBe(true);
    fireEvent.click(back);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("locks during send and shows reselect error", () => {
    render(
      <RouterModelPicker
        models={MODELS}
        selectedId={3}
        disabled
        error="沒有此 Router 模型的使用權限，請重新選擇"
        onChange={() => {}}
      />,
    );
    expect(screen.getByLabelText("此則對話使用的模型，僅由 ANILA 自動派工時可選")).toBeDisabled();
    expect(screen.getByText(/請重新選擇/)).toBeTruthy();
  });
});
