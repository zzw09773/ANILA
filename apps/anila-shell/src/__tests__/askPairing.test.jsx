import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { MessageBubble } from "../chat.jsx";

afterEach(cleanup);

const Q1 = "這份報告的主題與型別是？";
const Q2 = "這份技術評估報告的主題是什麼？";
const REPORT = "量子計算對密碼學之衝擊";

function answeredAsk(id, question, answer, options = []) {
  return {
    interrupt_id: id,
    kind: "ask_user",
    status: "answered",
    payload: {
      question,
      options,
      allow_other: true,
    },
    answer,
  };
}

function renderTurn(msg) {
  render(
    <MessageBubble
      msg={{
        id: "a1",
        role: "assistant",
        streaming: false,
        ...msg,
      }}
      agents={[]}
      conversationId={1}
    />,
  );
}

describe("連續 ASK 收合後題目與答案要成對", () => {
  it("每一題各自顯示題目與回答，題目不進答案正文", () => {
    renderTurn({
      text: `${Q1}\n${Q2}\n${REPORT}`,
      prefaceText: Q1,
      resumeText: `${Q2}\n${REPORT}`,
      settledInterrupts: [
        answeredAsk("i1", Q1, { selected: ["技術評估"], other_text: "" }, [
          { label: "技術評估", value: "技術評估", description: "" },
        ]),
      ],
      interrupt: answeredAsk("i2", Q2, {
        selected: [],
        other_text: "量子計算在密碼學的應用",
      }),
      thinkingStatus: "complete",
      thinkingElapsedMs: 33000,
      thinkingSummaries: [{ text: "整理報告架構", at: 1 }],
      reasoning: "",
    });

    const questions = screen.getAllByTestId("interrupt-question").map((el) => el.textContent);
    const summaries = screen.getAllByTestId("interrupt-summary").map((el) => el.textContent);
    expect(questions).toEqual([Q1, Q2]);
    expect(summaries).toEqual([
      "已選擇：技術評估",
      "補充：量子計算在密碼學的應用",
    ]);
    const bodies = [...document.querySelectorAll(".anila-msg-body")]
      .map((el) => el.textContent)
      .join("\n");
    expect(bodies).toContain(REPORT);
    expect(bodies).not.toContain(Q1);
    expect(bodies).not.toContain(Q2);
    expect(screen.queryByRole("button", { name: "原始思考" })).toBeNull();
  });

  it("三題與複選的補充都留在自己的題上", () => {
    const q3 = "要附上哪些比較？";
    renderTurn({
      text: `${Q1}\n${Q2}\n${q3}\n${REPORT}`,
      prefaceText: Q1,
      resumeText: `${Q2}\n${q3}\n${REPORT}`,
      settledInterrupts: [
        answeredAsk("i1", Q1, { selected: ["技術評估"], other_text: "" }, [
          { label: "技術評估", value: "技術評估", description: "" },
        ]),
        answeredAsk("i2", Q2, { selected: [], other_text: "量子計算在密碼學的應用" }),
      ],
      interrupt: {
        ...answeredAsk("i3", q3, {
          selected: ["威脅模型", "實作成本"],
          other_text: "含遷移步驟",
        }, [
          { label: "威脅模型", value: "威脅模型", description: "" },
          { label: "實作成本", value: "實作成本", description: "" },
        ]),
        payload: {
          question: q3,
          options: [
            { label: "威脅模型", value: "威脅模型", description: "" },
            { label: "實作成本", value: "實作成本", description: "" },
          ],
          multi: true,
          allow_other: true,
        },
      },
    });

    expect(screen.getAllByTestId("interrupt-question").map((el) => el.textContent)).toEqual([
      Q1, Q2, q3,
    ]);
    expect(screen.getAllByTestId("interrupt-summary").map((el) => el.textContent)).toEqual([
      "已選擇：技術評估",
      "補充：量子計算在密碼學的應用",
      "已選擇：威脅模型、實作成本；補充：含遷移步驟",
    ]);
    const bodies = [...document.querySelectorAll(".anila-msg-body")]
      .map((el) => el.textContent)
      .join("\n");
    expect(bodies).toContain(REPORT);
    expect(bodies).not.toContain(q3);
  });

  it("舊對話只拿掉中斷邊界上的題目，答案裡重複的標題留著", () => {
    renderTurn({
      text: `${Q1}\n${Q2}\n${Q2}\n${REPORT}`,
      prefaceText: Q1,
      resumeText: `${Q2}\n${Q2}\n${REPORT}`,
      settledInterrupts: [
        answeredAsk("i1", Q1, { selected: ["技術評估"], other_text: "" }, [
          { label: "技術評估", value: "技術評估", description: "" },
        ]),
      ],
      interrupt: answeredAsk("i2", Q2, {
        selected: [],
        other_text: "量子計算在密碼學的應用",
      }),
    });

    const bodies = [...document.querySelectorAll(".anila-msg-body")]
      .map((el) => el.textContent)
      .join("\n");
    expect(bodies).toContain(REPORT);
    expect(bodies).not.toContain(Q1);
    expect(bodies.split(Q2).length - 1).toBe(1);
  });
});
