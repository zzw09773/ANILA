import { describe, expect, it } from "vitest";

import { buildPersistMeta } from "../runtime/messageMeta.js";
import { applyServerPath } from "../runtime/messageTree.js";
import { dispatchSseEvent } from "../runtime/sse.js";
import {
  THINKING_STAGE_MARK,
  applyThinkingStage,
  readThinkingStages,
  settleThinkingStages,
} from "../runtime/thinkingStages.js";

const prior = [
  { index: 0, title: "拆解需求", status: "done", startedAt: 1, endedAt: 2 },
];

describe("思考階段清單", () => {
  it("把 anila.thinking_stage 送到回呼，不把它當成答案", () => {
    const onThinkingStage = viOn();
    const acc = { text: "", add(piece) { this.text += piece; }, get() { return this.text; } };
    dispatchSseEvent(
      {
        event: "anila.thinking_stage",
        data: '{"index":0,"title":"拆解需求","status":"running"}',
      },
      { onThinkingStage, accumulator: acc },
    );
    expect(onThinkingStage.calls[0]).toEqual({
      index: 0,
      title: "拆解需求",
      status: "running",
    });
    expect(acc.get()).toBe("");
  });

  it("ASK 續答把新階段接在同一則訊息後面，編號繼續", () => {
    const running = applyThinkingStage(
      prior,
      { index: 0, title: "撰寫第一章", status: "running" },
      { base: prior.length, now: 10 },
    );
    expect(running.map((row) => row.title)).toEqual(["拆解需求", "撰寫第一章"]);
    expect(running[1]).toMatchObject({ index: 1, status: "running", startedAt: 10 });
    const done = applyThinkingStage(
      running,
      { index: 0, title: "撰寫第一章", status: "done" },
      { base: prior.length, now: 20 },
    );
    expect(done[0]).toMatchObject({ title: "拆解需求", status: "done" });
    expect(done[1]).toMatchObject({ index: 1, title: "撰寫第一章", status: "done", endedAt: 20 });
  });

  it("停止把進行中的階段收成 stopped，已完成的不動", () => {
    const running = applyThinkingStage(
      prior,
      { index: 0, title: "撰寫第一章", status: "running" },
      { base: 1, now: 10 },
    );
    const stopped = settleThinkingStages(running, "stopped", 30);
    expect(stopped[0].status).toBe("done");
    expect(stopped[1].status).toBe("stopped");
    expect(stopped[1].endedAt).toBe(30);
    expect(THINKING_STAGE_MARK.stopped).toBe("■");
    expect(THINKING_STAGE_MARK.done).toBe("✓");
    expect(THINKING_STAGE_MARK.running).toBe("⋯");
    expect(THINKING_STAGE_MARK.error).toBe("✕");
  });

  it("階段隨訊息存檔，重新載入讀得回來", () => {
    const stages = [
      { index: 0, title: "拆解需求", status: "done", startedAt: 1, endedAt: 2 },
      { index: 1, title: "撰寫第一章", status: "stopped", startedAt: 10, endedAt: 30 },
    ];
    const meta = buildPersistMeta(
      { reasoning: "先看題目。" },
      { reasoning: "先看題目。", thinkingStages: stages },
    );
    expect(meta.thinking_stages).toEqual(stages);
    expect(meta.reasoning).toBe("先看題目。");
    expect(readThinkingStages(meta)).toEqual(stages);

    const fromServer = applyServerPath(
      [{ id: "local", dbId: 4, role: "assistant", text: "舊", thinkingStages: [] }],
      [{
        id: "srv-4",
        dbId: 4,
        role: "assistant",
        text: "第一章草稿。",
        thinkingStages: readThinkingStages({ thinking_stages: stages }),
      }],
      9,
    );
    expect(fromServer[0].thinkingStages).toEqual(stages);
    expect(fromServer[0].text).toBe("第一章草稿。");
  });

  it("同一輪裡相同標題不再新增，舊訊息裡重複的階段也只留第一次", () => {
    const titles = [
      "需求與設計假設",
      "增益波束與 EIRP 計算",
      "功耗與散熱估算",
      "風險與驗證計畫",
    ];
    let stages = [];
    titles.forEach((title, index) => {
      stages = applyThinkingStage(
        stages,
        { index, title, status: "running" },
        { now: index + 1 },
      );
      if (index > 0) {
        stages = applyThinkingStage(
          stages,
          { index: index - 1, title: titles[index - 1], status: "done" },
          { now: index + 1 },
        );
      }
    });
    stages = applyThinkingStage(
      stages,
      { index: 4, title: "  需求與設計假設  ", status: "running" },
      { now: 10 },
    );
    stages = applyThinkingStage(
      stages,
      { index: 3, title: "風險與驗證計畫", status: "done" },
      { now: 11 },
    );
    stages = applyThinkingStage(
      stages,
      { index: 5, title: "第 2 輪：寫水星", status: "running" },
      { now: 12 },
    );
    stages = applyThinkingStage(
      stages,
      { index: 6, title: "搜尋過往對話", status: "done" },
      { now: 13 },
    );
    stages = applyThinkingStage(
      stages,
      { index: 7, title: "整理答案", status: "running" },
      { now: 14 },
    );
    expect(stages.map((row) => row.title)).toEqual([
      ...titles,
      "第 2 輪：寫水星",
      "搜尋過往對話",
      "整理答案",
    ]);
    expect(stages.find((row) => row.title === "需求與設計假設").status).toBe("done");
    expect(stages.find((row) => row.title === "風險與驗證計畫").status).toBe("done");

    const duplicated = [
      ...titles.map((title, index) => ({ index, title, status: "done" })),
      ...titles.map((title, index) => ({
        index: index + titles.length,
        title: `  ${title}  `,
        status: "done",
      })),
    ];
    expect(readThinkingStages({ thinking_stages: duplicated }).map((row) => row.title)).toEqual(titles);
    const restored = applyServerPath(
      [],
      [{
        id: "srv-8",
        dbId: 8,
        role: "assistant",
        text: "答案。",
        thinkingStages: readThinkingStages({ thinking_stages: duplicated }),
      }],
      9,
    );
    expect(restored[0].thinkingStages.map((row) => row.title)).toEqual(titles);
  });

  it("這則還在本地累積、比伺服器多的階段不會被重新整理蓋掉", () => {
    const live = [
      { index: 0, title: "拆解需求", status: "done" },
      { index: 1, title: "撰寫第一章", status: "running" },
    ];
    const next = applyServerPath(
      [{ id: "srv-5", dbId: 5, role: "assistant", text: "半", thinkingStages: live }],
      [{ id: "srv-5", dbId: 5, role: "assistant", text: "半", thinkingStages: [live[0]] }],
      9,
    );
    expect(next[0].thinkingStages).toEqual(live);
  });
});

function viOn() {
  const fn = (payload) => {
    fn.calls.push(payload);
  };
  fn.calls = [];
  return fn;
}
