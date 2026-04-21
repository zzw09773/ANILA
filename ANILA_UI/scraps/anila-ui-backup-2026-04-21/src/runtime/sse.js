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
  apiKey,
  payload,
  onText,
  onTrace,
  onMeta,
  onJson,
}) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
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
