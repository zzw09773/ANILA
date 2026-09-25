import { describe, expect, it } from "vitest";

import { mergeLiveTrace } from "../runtime/trace.js";

describe("mergeLiveTrace", () => {
  it("終態召回更新同一階段，不另留一筆仍在搜尋的步驟", () => {
    const running = {
      kind: "recall",
      label: "搜尋過往對話",
      status: "running",
      at: 10,
    };
    const done = {
      kind: "recall",
      label: "搜尋過往對話",
      status: "done",
      at: 40,
    };
    const trace = mergeLiveTrace(mergeLiveTrace([], running), done);
    expect(trace).toHaveLength(1);
    expect(trace[0].status).toBe("done");
    expect(trace[0].at).toBe(10);
  });

  it("召回失敗時同一階段改成 error", () => {
    const trace = mergeLiveTrace(
      [{ kind: "recall", label: "搜尋過往對話", status: "running", at: 1 }],
      { kind: "recall", label: "搜尋過往對話", status: "error", at: 2 },
    );
    expect(trace).toEqual([
      { kind: "recall", label: "搜尋過往對話", status: "error", at: 1 },
    ]);
  });
});
