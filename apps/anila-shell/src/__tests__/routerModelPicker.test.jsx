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
    fireEvent.click(screen.getByLabelText("此則對話使用的模型，僅自動選助手時可選"));
    expect(screen.getAllByText("GLM").length).toBeGreaterThan(0);
    expect(screen.getByText("全院預設")).toBeTruthy();
    expect(screen.getByText("Qwen")).toBeTruthy();
    expect(screen.getByText("不穩")).toBeTruthy();
    expect(screen.queryByText(/anila-router/)).toBeNull();
    expect(screen.queryByText(/ANILA 自動選助手/)).toBeNull();
  });

  it("saves a new selection", () => {
    const onChange = vi.fn();
    render(
      <RouterModelPicker models={MODELS} selectedId={3} defaultModelId={3} onChange={onChange} />,
    );
    fireEvent.click(screen.getByLabelText("此則對話使用的模型，僅自動選助手時可選"));
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
    const trigger = screen.getByLabelText("此則對話使用的模型，僅自動選助手時可選");
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent("Qwen 3.8 Flash");
    expect(screen.queryByText("沒有可用模型")).toBeNull();
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
    expect(screen.getByLabelText("此則對話使用的模型，僅自動選助手時可選")).toBeDisabled();
    expect(screen.getByText(/請重新選擇/)).toBeTruthy();
  });
});
