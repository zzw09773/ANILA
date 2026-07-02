import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiKeyGate, MessageBubble } from "./components.jsx";

const agents = [
  { id: "anila-router", name: "ANILA Router", short: "auto" },
  { id: "hr-policy", name: "HR Policy", short: "hr" },
];

describe("runtime components", () => {
  it("submits API key gate action", () => {
    const onSubmit = vi.fn();
    render(
      <ApiKeyGate
        error=""
        apiKeyDraft="sk-demo"
        setApiKeyDraft={() => {}}
        onSubmit={onSubmit}
        loading={false}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /驗證並進入/i }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("opens citations from assistant message", () => {
    const onOpenCitation = vi.fn();
    render(
      <MessageBubble
        agents={agents}
        onOpenCitation={onOpenCitation}
        onPickFollowUp={() => {}}
        message={{
          id: "a-1",
          role: "assistant",
          text: "依規定 [1] 可申請特休",
          streaming: false,
          routedAgentId: "hr-policy",
          citations: [
            {
              id: "cit-1",
              title: "員工手冊",
              section: "4.7 特休",
              snippet: "到職滿六個月可申請特休",
              source_uri: "doc://hr-handbook",
            },
          ],
          trace: [],
          followUps: [],
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /查看 1 筆來源/i }));
    expect(onOpenCitation).toHaveBeenCalledTimes(1);
  });
});
