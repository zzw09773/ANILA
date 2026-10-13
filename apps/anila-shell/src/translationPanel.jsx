import React, { useMemo, useRef, useState } from "react";
import { Modal } from "./components.jsx";
import { requestTranslation } from "./runtime/translationRequest.js";
import {
  MODE_EXTERNAL,
  MODE_LABELS,
  MODE_READING,
  SYNTHETIC_EXAMPLE,
  TARGET_LANGUAGES,
  annotateTranslation,
  checkTranslation,
} from "./runtime/translationFidelity.js";

const fieldStyle = {
  width: "100%",
  minHeight: 120,
  resize: "vertical",
  boxSizing: "border-box",
  padding: "8px 10px",
  fontSize: 14,
  lineHeight: 1.55,
  color: "var(--fg)",
  background: "var(--bg)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
  fontFamily: "inherit",
};

const quietButton = {
  padding: "6px 12px",
  borderRadius: "var(--radius)",
  border: "1px solid var(--border)",
  background: "var(--bg-elev)",
  color: "var(--fg)",
  cursor: "pointer",
};

const redBox = {
  background: "color-mix(in oklch, var(--danger) 16%, transparent)",
  boxShadow: "inset 0 0 0 2px var(--danger)",
  borderRadius: 4,
  padding: "0 3px",
  color: "inherit",
};

function translationErrorText(err) {
  const msg = typeof err?.message === "string" ? err.message.trim() : "";
  if (msg.includes("用量已達上限")) return msg;
  return "這次沒有取得譯文。沒有譯文就不會顯示檢查結果。";
}

function CheckedTranslation({ translation, mismatches }) {
  const segments = annotateTranslation(translation, mismatches);
  return (
    <div
      data-testid="translation-checked"
      style={{ fontSize: 14, lineHeight: 1.65, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
    >
      {segments.map((segment, index) => {
        if (!segment.hits.length) return <span key={index}>{segment.text}</span>;
        const title = segment.hits.map((hit) => hit.message).join("\n");
        return (
          <mark
            key={index}
            data-testid="translation-mismatch"
            data-kind={segment.hits.map((hit) => hit.kind).join(" ")}
            title={title}
            style={redBox}
          >
            {segment.text}
          </mark>
        );
      })}
    </div>
  );
}

export function TranslationPanel({ open, onClose, translateText }) {
  const [mode, setMode] = useState(MODE_READING);
  const [targetId, setTargetId] = useState(TARGET_LANGUAGES[0].id);
  const [source, setSource] = useState("");
  const [translation, setTranslation] = useState("");
  const [exampleNote, setExampleNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const abortRef = useRef(null);
  const targetLabel = TARGET_LANGUAGES.find((item) => item.id === targetId)?.label || TARGET_LANGUAGES[0].label;
  const external = mode === MODE_EXTERNAL;
  const report = useMemo(() => {
    if (!external || !source.trim() || !translation.trim()) return null;
    return checkTranslation(source, translation);
  }, [external, source, translation]);

  function close() {
    seq.current += 1;
    abortRef.current?.abort();
    onClose?.();
  }

  async function onTranslate() {
    const text = source.trim();
    if (!text || busy) return;
    const id = seq.current + 1;
    seq.current = id;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    setError("");
    setExampleNote("");
    try {
      const run = translateText || requestTranslation;
      const next = await run({ source: text, targetLabel, signal: controller.signal });
      if (id !== seq.current || controller.signal.aborted) return;
      setTranslation(typeof next === "string" ? next : "");
    } catch (err) {
      if (err?.name === "AbortError" || id !== seq.current) return;
      setError(translationErrorText(err));
    } finally {
      if (id === seq.current) setBusy(false);
    }
  }

  function loadExample() {
    seq.current += 1;
    abortRef.current?.abort();
    setBusy(false);
    setError("");
    setSource(SYNTHETIC_EXAMPLE.source);
    setTranslation(SYNTHETIC_EXAMPLE.translation);
    setExampleNote(SYNTHETIC_EXAMPLE.note);
  }

  function clearAll() {
    seq.current += 1;
    abortRef.current?.abort();
    setBusy(false);
    setError("");
    setExampleNote("");
    setSource("");
    setTranslation("");
  }

  const clearOfIssues = report && report.mismatches.length === 0 && report.unsettled.length === 0;

  return (
    <Modal
      open={open}
      onClose={close}
      title="翻譯"
      subtitle="快速閱讀是給自己看懂；對外使用是要寄出的譯文，程式會核對原文和譯文。"
      width={760}
    >
      <div data-testid="translation-panel" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <fieldset style={{ border: "1px solid var(--border)", borderRadius: "var(--radius)", margin: 0, padding: "8px 12px" }}>
          <legend style={{ padding: "0 6px", fontSize: 12, color: "var(--fg-muted)" }}>翻譯模式</legend>
          <div role="radiogroup" aria-label="翻譯模式" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {[MODE_READING, MODE_EXTERNAL].map((id) => (
              <label key={id} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13, lineHeight: 1.5 }}>
                <input
                  type="radio"
                  name="translation-mode"
                  value={id}
                  checked={mode === id}
                  onChange={() => setMode(id)}
                  style={{ marginTop: 3 }}
                />
                <span>
                  <span style={{ fontWeight: 600 }}>{MODE_LABELS[id]}</span>
                  <span style={{ display: "block", color: "var(--fg-muted)" }}>
                    {id === MODE_READING
                      ? "給自己看：快速出整篇譯文，方便讀懂內容。不做數字、單位、否定詞檢查。"
                      : "要寄出、公告或交給別人的文字：譯完由程式核對數字、單位、否定詞，對不上用紅框標出；同一個詞有兩種譯法，標「未定譯」。"}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <p style={{ margin: 0, fontSize: 12, color: "var(--fg-muted)", lineHeight: 1.5 }}>
          檢查的是這段原文和譯文，不是回答和引用來源。模型只負責翻譯，不負責判定對不上。譯文留在這個視窗，不會寫進對話。
        </p>

        <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 12, color: "var(--fg-muted)" }}>
          譯成
          <select
            aria-label="譯成"
            value={targetId}
            onChange={(event) => setTargetId(event.target.value)}
            style={{ ...fieldStyle, minHeight: 0, padding: "6px 8px" }}
          >
            {TARGET_LANGUAGES.map((item) => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 12, color: "var(--fg-muted)" }}>
          原文
          <textarea
            aria-label="原文"
            value={source}
            onChange={(event) => { setSource(event.target.value); setExampleNote(""); }}
            placeholder="貼上要翻譯的文字"
            style={fieldStyle}
          />
        </label>

        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          <button
            type="button"
            onClick={onTranslate}
            disabled={busy || !source.trim()}
            style={{
              padding: "6px 12px",
              borderRadius: "var(--radius)",
              border: "1px solid var(--accent)",
              background: "var(--accent)",
              color: "var(--accent-fg)",
              cursor: busy || !source.trim() ? "not-allowed" : "pointer",
              opacity: busy || !source.trim() ? 0.55 : 1,
            }}
          >
            {busy ? "翻譯中…" : "翻譯"}
          </button>
          <button type="button" onClick={loadExample} style={quietButton}>載入合成範例</button>
          <button type="button" onClick={clearAll} style={quietButton}>清除</button>
        </div>

        {exampleNote ? (
          <p data-testid="translation-example-note" style={{ margin: 0, fontSize: 12, color: "var(--fg-muted)" }}>{exampleNote}</p>
        ) : null}
        {error ? (
          <p role="alert" style={{ margin: 0, fontSize: 13, color: "var(--danger)", lineHeight: 1.5 }}>{error}</p>
        ) : null}

        <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 12, color: "var(--fg-muted)" }}>
          譯文
          <textarea
            aria-label="譯文"
            value={translation}
            onChange={(event) => { setTranslation(event.target.value); setExampleNote(""); }}
            placeholder="譯文會出現在這裡。也可以自己貼上。對外使用會立刻重新檢查。"
            style={{ ...fieldStyle, minHeight: 88 }}
          />
        </label>

        {external && translation.trim() ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span style={{ fontSize: 12, color: "var(--fg-muted)" }}>檢查結果</span>
            <CheckedTranslation translation={translation} mismatches={report?.mismatches || []} />
          </div>
        ) : null}

        {mode === MODE_READING && translation.trim() ? (
          <p style={{ margin: 0, fontSize: 12, color: "var(--fg-muted)" }}>快速閱讀不做檢查。</p>
        ) : null}

        {external && !translation.trim() ? (
          <p style={{ margin: 0, fontSize: 12, color: "var(--fg-muted)" }}>譯文出來之後才檢查。</p>
        ) : null}

        {report ? (
          <div aria-live="polite" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {clearOfIssues ? (
              <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5 }}>
                {report.terminologyChecked
                  ? "數字、單位和否定詞都對得上，沒有未定譯。"
                  : "數字、單位和否定詞都對得上。這個環境無法斷詞，所以沒有檢查未定譯。"}
              </p>
            ) : null}
            {report.mismatches.length > 0 ? (
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>對不上的地方</div>
                <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
                  {report.mismatches.map((item, index) => (
                    <li
                      key={`${item.kind}-${index}`}
                      data-testid="translation-mismatch-note"
                      style={{
                        ...redBox,
                        padding: "6px 8px",
                        fontSize: 13,
                        lineHeight: 1.5,
                        boxShadow: "inset 0 0 0 2px var(--danger)",
                      }}
                    >
                      {item.message}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {report.unsettled.length > 0 ? (
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>未定譯</div>
                <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
                  {report.unsettled.map((item) => (
                    <li key={item.term} data-testid="translation-unsettled" style={{ fontSize: 13, lineHeight: 1.5 }}>
                      <span style={{
                        display: "inline-block",
                        marginRight: 6,
                        padding: "0 6px",
                        border: "1px solid var(--warn, var(--danger))",
                        borderRadius: 999,
                        fontSize: 12,
                      }}>未定譯</span>
                      {item.message}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {report.mismatches.length > 0 && !report.terminologyChecked ? (
              <p style={{ margin: 0, fontSize: 12, color: "var(--fg-muted)" }}>這個環境無法斷詞，所以沒有檢查未定譯。</p>
            ) : null}
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
