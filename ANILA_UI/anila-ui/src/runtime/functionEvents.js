// SSE event handler for ANILA Functions v1 /run.
//
// CSP streams text/event-stream chunks shaped per spec §4.5:
//
//   event: function_event
//   data: {"type": "status" | "host_command" | "message" | "citation" | "error", ...}
//
//   event: function_done
//   data: {"run_id": ..., "status": "success" | "error" | "timeout"}
//
// This module owns the parser + dispatcher. `host_command` events go
// through `runtime/hostCommands.js`'s whitelist; everything else maps
// to UI state actions provided by the caller via `ctx`.

import { dispatchHostCommand } from "./hostCommands.js";

export async function consumeFunctionEventStream(response, ctx) {
  if (!response.body) {
    throw new Error("response has no readable body");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let doneInfo = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by "\n\n"
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const parsed = parseFrame(frame);
      if (!parsed) continue;
      if (parsed.event === "function_event") {
        dispatchEventPayload(parsed.data, ctx);
      } else if (parsed.event === "function_done") {
        doneInfo = parsed.data;
      }
    }
  }
  return doneInfo;
}

function parseFrame(frame) {
  let event = null;
  let dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!event) return null;
  let data = null;
  if (dataLines.length) {
    try {
      data = JSON.parse(dataLines.join("\n"));
    } catch (_) {
      data = { raw: dataLines.join("\n") };
    }
  }
  return { event, data };
}

function dispatchEventPayload(payload, ctx) {
  if (!payload || typeof payload !== "object") return;
  switch (payload.type) {
    case "status":
      ctx.onStatus?.({
        description: payload.description,
        done: !!payload.done,
      });
      return;
    case "host_command":
      dispatchHostCommand(payload.verb, payload.args, ctx);
      return;
    case "message":
      ctx.onMessage?.({ content: payload.content || "" });
      return;
    case "citation":
      ctx.citations?.open?.(payload.payload || payload.citation || {});
      return;
    case "error":
      ctx.onError?.(payload.message || "function error");
      return;
    default:
      // eslint-disable-next-line no-console
      console.warn("[ANILA Functions] unknown event type:", payload.type);
  }
}
