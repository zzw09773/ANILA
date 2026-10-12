import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { EmptyState } from "../components/EmptyState.jsx";

describe("EmptyState verified starter prompts", () => {
  it("shows only retrieval-checked sample questions", () => {
    const onPick = vi.fn();
    render(
      <EmptyState
        agent={{ id: "anila-router", name: "ANILA" }}
        agents={[{ id: "anila-router", name: "ANILA" }, { id: "demo", name: "示範助手" }]}
        onPick={onPick}
      />,
    );

    expect(screen.getByRole("button", { name: /會議室可以借多久/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /文件要保存幾年/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /訪客攝影要先做什麼/ })).toBeTruthy();
    expect(screen.queryByText("停車位向誰申請？")).toBeNull();
    expect(screen.queryByText("ANILA 可以做什麼？")).toBeNull();
    expect(screen.getByText(/合成範例。程式已確認查得到最長時數/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /會議室可以借多久/ }));
    expect(onPick).toHaveBeenCalledWith("示範文件裡，會議室每次最長可以借幾小時？");
  });
});
