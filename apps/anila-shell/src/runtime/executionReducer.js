const TERMINAL = new Set(["completed", "failed", "cancelled"]);

function latestSteps(events) {
  const byId = new Map();
  for (const event of events) {
    if (!event?.step_id || !Number.isInteger(event.sequence)) continue;
    const current = byId.get(event.step_id);
    if (!current || event.sequence > current.sequence) byId.set(event.step_id, event);
  }
  return [...byId.values()].sort((a, b) => a.sequence - b.sequence);
}

function deriveStatus(steps) {
  const terminalAgent = [...steps].reverse().find(
    (step) => step.kind === "agent" && TERMINAL.has(step.status),
  );
  if (terminalAgent) return terminalAgent.status;
  if (steps.some((step) => step.status === "failed")) return "failed";
  if (steps.some((step) => step.status === "cancelled")) return "cancelled";
  return steps.length ? "running" : "idle";
}

export function reduceExecution(message, action) {
  if (action?.type !== "backend_step" || !action.event) return message;
  const events = [...(message.executionEvents || []), action.event];
  const steps = latestSteps(events);
  const current = steps.at(-1);
  return {
    ...message,
    executionEvents: events,
    executionSteps: steps,
    executionStatus: deriveStatus(steps),
    // Counts and labels are projections of backend events only; no timers or
    // client-side guessed phases are allowed to mutate these fields.
    timelineEventCount: events.length,
    completedStepCount: steps.filter((step) => step.status === "completed").length,
    stageLabel:
      current?.safe_output_summary || current?.safe_input_summary || current?.kind || null,
    stage: events.length,
    trace: steps.map((step) => ({
      id: step.step_id,
      type: step.kind,
      label: step.tool_name || step.kind,
      detail: step.safe_output_summary || step.safe_input_summary || "",
      status: step.status,
      latency_ms: step.latency_ms,
      backendEvent: true,
    })),
  };
}

export function executionCallbacks(apply) {
  return {
    onStep: (event) => apply({ type: "backend_step", event }),
  };
}
