// Export a conversation as Markdown or JSON and hand it to the browser as a
// download. Pure front-end, works offline. `msgs` are `{ role, text }` rows.

export function downloadConversationExport(title, msgs, format) {
  let content; let mime; let ext;
  if (format === "markdown") {
    const lines = [`# ${title}`, ""];
    for (const m of msgs) {
      if (!m.text) continue;
      lines.push(m.role === "user" ? "## 使用者" : "## ANILA");
      lines.push("", m.text, "");
    }
    content = lines.join("\n"); mime = "text/markdown"; ext = "md";
  } else {
    content = JSON.stringify({ title, messages: msgs.map((m) => ({ role: m.role, content: m.text })) }, null, 2);
    mime = "application/json"; ext = "json";
  }
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${title.replace(/[^\w一-鿿 -]/g, "_").slice(0, 40)}.${ext}`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
