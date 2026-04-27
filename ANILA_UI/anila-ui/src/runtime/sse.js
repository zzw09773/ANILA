export function parseSseBlocks(buffer) {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const hasTerminatingSeparator = normalized.endsWith("\n\n");
  const remainder = hasTerminatingSeparator ? "" : blocks.pop() || "";
  const events = blocks
    .map((block) => parseSseEvent(block))
    .filter(Boolean);
  return { events, remainder };
}

export function parseSseEvent(block) {
  if (!block.trim()) {
    return null;
  }

  let event = "message";
  const dataLines = [];

  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  const data = dataLines.join("\n");
  return { event, data, raw: block };
}

export async function streamChatCompletion({
  url,
  payload,
  onText,
  onTrace,
  onMeta,
  onJson,
  onReasoning,
}) {
  // Sprint 7 X follow-up：SPA 完全走 httpOnly cookie + double-submit CSRF。
  // `apiKey` parameter 已移除，避免讓呼叫端誤以為前端可以管理 key（dead
  // 路徑也是攻擊面）。SDK / curl 用戶請改打 ``apiKeyRequest`` 或自己組
  // Authorization header — 那不會經過此函式。
  const headers = { "Content-Type": "application/json" };
  if (typeof document !== "undefined") {
    const match = document.cookie.match(/(?:^|;\s*)anila_csrf=([^;]+)/);
    if (match) headers["X-CSRF-Token"] = decodeURIComponent(match[1]);
  }
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers,
    body: JSON.stringify({ ...payload, stream: true }),
  });

  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(detail || "Streaming failed");
    error.status = response.status;
    throw error;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Readable stream unavailable");
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let accumulatedText = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseBlocks(buffer);
    buffer = parsed.remainder;

    for (const event of parsed.events) {
      if (event.data === "[DONE]") {
        continue;
      }
      if (event.event === "anila.trace") {
        onTrace?.(JSON.parse(event.data));
        continue;
      }
      if (event.event === "anila.meta") {
        onMeta?.(JSON.parse(event.data));
        continue;
      }
      if (event.event === "anila.reasoning") {
        try {
          const payload = JSON.parse(event.data);
          if (payload?.delta) onReasoning?.(payload.delta);
        } catch {
          // ignore malformed reasoning frame
        }
        continue;
      }
      const chunk = JSON.parse(event.data);
      onJson?.(chunk);
      const delta = chunk.choices?.[0]?.delta?.content || "";
      if (delta) {
        accumulatedText += delta;
        onText?.(accumulatedText);
      }
    }
  }

  return accumulatedText;
}
