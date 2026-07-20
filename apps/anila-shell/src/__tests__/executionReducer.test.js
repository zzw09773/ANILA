import { describe, expect, it, vi } from "vitest";

import { executionCallbacks, reduceExecution } from "../runtime/executionReducer.js";

const step = (sequence, status, kind = "tool") => ({
  schema_version: "step-event/v1",
  event_id: `e${sequence}`,
  sequence,
  cursor: String(sequence),
  trace_id: "trace",
  task_id: "task",
  session_id: "session",
  invocation_id: "invocation",
  run_id: "run",
  step_id: kind === "agent" ? "agent:1" : "tool:1",
  parent_step_id: null,
  depends_on: [],
  kind,
  status,
  safe_input_summary: status === "running" ? "開始工具" : null,
  safe_output_summary: status !== "running" ? `工具${status}` : null,
  agent_id: "1",
  tool_name: kind === "tool" ? "search_documents" : null,
  retry_count: 0,
  classification: "無機密",
});

describe("single execution reducer", () => {
  it("derives status, counts and labels only from backend StepEvents", () => {
    let message = { id: "a", trace: [], stage: 999, stageLabel: "client guess" };
    message = reduceExecution(message, { type: "backend_step", event: step(1, "running") });
    message = reduceExecution(message, { type: "backend_step", event: step(2, "completed") });
    message = reduceExecution(message, {
      type: "backend_step",
      event: step(3, "completed", "agent"),
    });
    expect(message.executionStatus).toBe("completed");
    expect(message.timelineEventCount).toBe(3);
    expect(message.completedStepCount).toBe(2);
    expect(message.stage).toBe(3);
    expect(message.stageLabel).toBe("工具completed");
    expect(message.trace.every((item) => item.backendEvent)).toBe(true);
  });

  it("exposes the same callback shape to every message path", () => {
    const apply = vi.fn();
    executionCallbacks(apply).onStep(step(1, "running"));
    expect(apply).toHaveBeenCalledWith({ type: "backend_step", event: step(1, "running") });
  });
});
