import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { normalizeAgents } from "../app.jsx";
import { AgentSelector } from "../chat.jsx";

describe("平台對話入口", () => {
  it("顯示 ANILA，短名仍是 auto，代表交給 ANILA 決定要不要派給助手", () => {
    const agents = normalizeAgents([]);
    render(
      <AgentSelector agents={agents} value="anila-router" onChange={() => {}} />,
    );
    expect(screen.getByText("ANILA")).toBeTruthy();
    expect(screen.getByText("auto")).toBeTruthy();
    expect(screen.queryByText("ANILA 自動選助手")).toBeNull();
    expect(agents[0].short).toBe("auto");
  });
});
