import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { HandoverInterviewDialog } from "../handoverInterview.jsx";
import { ShellNav } from "../shellNav.jsx";
import {
  formatChecklistText,
  mayRequestChecklist,
  requestChecklist,
} from "../runtime/handoverInterview.js";

const FIRST = "接手的人最可能因為不知道什麼而出錯？";
const SECOND = "還有哪些事還沒決定，接手的人不要自己先做？";

const QUESTIONS = [
  {
    id: "unknown_pitfall",
    topic: "pitfall",
    heading: "踩過的坑",
    prompt: FIRST,
    placeholder: "例如：週報數字要先對過。",
  },
  {
    id: "open_decisions",
    topic: "decision",
    heading: "還沒決定的事",
    prompt: SECOND,
    placeholder: "例如：下季頻率還沒定。",
  },
];

function interview(overrides = {}) {
  return {
    id: 1,
    project_label: "週報彙整",
    status: "answering",
    questions: QUESTIONS,
    answers: { unknown_pitfall: "", open_decisions: "" },
    checklist: null,
    created_at: "2026-10-12T00:00:00Z",
    confirmed_at: null,
    ...overrides,
  };
}

function mockApi() {
  const calls = [];
  let current = null;
  const authRequest = vi.fn(async (path, options = {}) => {
    const method = options.method || "GET";
    calls.push({ method, path, options });
    if (method === "GET" && path === "/api/handover-interviews/questions") {
      return { questions: QUESTIONS };
    }
    if (method === "GET" && path === "/api/handover-interviews") {
      return { interviews: [] };
    }
    if (method === "POST" && path === "/api/handover-interviews") {
      const body = JSON.parse(options.body);
      current = interview({ project_label: body.project_label });
      return current;
    }
    if (method === "PUT" && path === "/api/handover-interviews/1/answers") {
      const body = JSON.parse(options.body);
      current = { ...current, answers: body.answers };
      return current;
    }
    if (method === "POST" && path === "/api/handover-interviews/1/confirm") {
      current = {
        ...current,
        status: "confirmed",
        confirmed_at: "2026-10-12T01:00:00Z",
        checklist: null,
      };
      return current;
    }
    if (method === "POST" && path === "/api/handover-interviews/1/checklist") {
      current = {
        ...current,
        status: "ready",
        checklist: {
          project_label: current.project_label,
          wording: "model",
          notice: "說明依你確認的回答整理。題目是固定的，沒有另出新題。",
          sections: [
            {
              topic: "pitfall",
              heading: "踩過的坑",
              items: [{ question_id: "unknown_pitfall", prompt: FIRST, note: "模型改寫的說明" }],
            },
          ],
        },
      };
      return current;
    }
    throw new Error(`unexpected ${method} ${path}`);
  });
  return { authRequest, calls };
}

function checklistCalls(calls) {
  return calls.filter((call) => call.path.endsWith("/checklist"));
}

describe("handover interview helpers", () => {
  it("refuses to request a checklist before confirmation", async () => {
    const authRequest = vi.fn();
    await expect(requestChecklist(authRequest, interview())).rejects.toThrow("確認前不會產生");
    await expect(requestChecklist(authRequest, null)).rejects.toThrow("確認前不會產生");
    expect(authRequest).not.toHaveBeenCalled();
    expect(mayRequestChecklist(interview({ status: "confirmed", checklist: null }))).toBe(true);
    expect(mayRequestChecklist(interview({ status: "confirmed", checklist: { sections: [] } }))).toBe(false);
    expect(mayRequestChecklist(interview({ status: "ready" }))).toBe(false);
  });

  it("formats only an existing checklist", () => {
    expect(formatChecklistText(null)).toBe("");
    expect(formatChecklistText({ answers: { unknown_pitfall: "沿用上週" } })).toBe("");
    const text = formatChecklistText({
      project_label: "週報彙整",
      notice: "沒有改寫。",
      sections: [
        { heading: "踩過的坑", items: [{ prompt: FIRST, note: "沿用上週會差一週。" }] },
      ],
    });
    expect(text).toContain("交接清單");
    expect(text).toContain(FIRST);
    expect(text).toContain("沿用上週會差一週。");
  });
});

describe("HandoverInterviewDialog", () => {
  it("asks the fixed prompts in API order and waits to build the checklist", async () => {
    const { authRequest, calls } = mockApi();
    render(<HandoverInterviewDialog open onClose={() => {}} authRequest={authRequest} />);

    expect(await screen.findByRole("button", { name: "開始訪談" })).toBeTruthy();
    expect(screen.queryByTestId("handover-checklist")).toBeNull();
    expect(screen.queryByRole("button", { name: "產生交接清單" })).toBeNull();

    fireEvent.change(screen.getByPlaceholderText("例如：週報彙整"), {
      target: { value: "週報彙整" },
    });
    fireEvent.click(screen.getByRole("button", { name: "開始訪談" }));

    const first = await screen.findByRole("textbox", { name: FIRST });
    const boxes = screen.getAllByRole("textbox");
    expect(boxes[0]).toBe(first);
    expect(boxes[1].getAttribute("aria-label")).toBe(SECOND);
    expect(screen.queryByTestId("handover-checklist")).toBeNull();

    fireEvent.change(first, { target: { value: "沿用上週會差一週。" } });
    fireEvent.click(screen.getByRole("button", { name: "確認內容無誤" }));

    expect(await screen.findByText("沿用上週會差一週。")).toBeTruthy();
    expect(screen.getByText("已確認。交接清單還沒產生。下面是你確認過的回答。")).toBeTruthy();
    expect(screen.queryByTestId("handover-checklist")).toBeNull();
    expect(screen.queryByText("模型改寫的說明")).toBeNull();
    expect(checklistCalls(calls)).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "產生交接清單" }));
    expect(await screen.findByTestId("handover-checklist")).toBeTruthy();
    expect(screen.getByText("模型改寫的說明")).toBeTruthy();
    expect(screen.getByText(/沒有另出新題/)).toBeTruthy();
    expect(checklistCalls(calls)).toHaveLength(1);
  });

  it("renders a reversed question list in the order the API sent", async () => {
    const reversed = [...QUESTIONS].reverse();
    const authRequest = vi.fn(async (path, options = {}) => {
      const method = options.method || "GET";
      if (method === "GET" && path === "/api/handover-interviews/questions") {
        return { questions: reversed };
      }
      if (method === "GET" && path === "/api/handover-interviews") {
        return {
          interviews: [interview({ questions: reversed, answers: { open_decisions: "", unknown_pitfall: "" } })],
        };
      }
      throw new Error(`unexpected ${method} ${path}`);
    });
    render(<HandoverInterviewDialog open onClose={() => {}} authRequest={authRequest} />);
    const first = await screen.findByRole("textbox", { name: SECOND });
    expect(screen.getAllByRole("textbox")[0]).toBe(first);
  });
});

describe("ShellNav handover entry", () => {
  it("opens the interview from the platform menu without adding it to the main entries", async () => {
    const { authRequest } = mockApi();
    render(<ShellNav user={{ role: "user" }} authRequest={authRequest} />);
    fireEvent.click(screen.getByRole("button", { name: "平台入口" }));
    fireEvent.click(screen.getByRole("button", { name: "交接訪談" }));

    expect(await screen.findByRole("dialog", { name: "交接訪談" })).toBeTruthy();
    expect(screen.getByText(/這不是把對話交給同事/)).toBeTruthy();
    expect(await screen.findByRole("button", { name: "開始訪談" })).toBeTruthy();
    expect(screen.queryByTestId("handover-checklist")).toBeNull();
    expect(screen.queryByRole("button", { name: "產生交接清單" })).toBeNull();
  });
});
