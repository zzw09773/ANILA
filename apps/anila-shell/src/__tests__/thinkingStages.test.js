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
