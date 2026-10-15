import React, { useState } from "react";

import {
  compareDocumentVersions,
  explainDocumentVersions,
  visibleExplanations,
} from "./runtime/documentDiff.js";

const KIND = {
  added: "新增",
  removed: "刪除",
  modified: "修改",
};

const fieldButton = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  minHeight: 36,
  padding: "6px 14px",
  borderRadius: "var(--radius)",
  border: "1px solid var(--border-strong)",
  background: "var(--bg-elev)",
  color: "var(--fg)",
  fontFamily: "var(--font-sans)",
  fontSize: 14,
  cursor: "pointer",
};

const primaryButton = {
  ...fieldButton,
  border: "1px solid transparent",
  background: "var(--accent)",
  color: "var(--accent-fg)",
};

function FileSlot({ which, label, file, onPick }) {
  const inputId = `doc-diff-${which}`;
  return (
    <div className="doc-diff-file" style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 0 }}>
      <span id={`${inputId}-label`} style={{ fontSize: 13, fontWeight: 600 }}>{label}</span>
      <input
        id={inputId}
        className="doc-diff-file__input"
        aria-labelledby={`${inputId}-label`}
        type="file"
        accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        onChange={(event) => onPick(which, event.target.files?.[0] || null)}
      />
      <label htmlFor={inputId} className="doc-diff-file__button" style={{ ...fieldButton, justifyContent: "flex-start", minWidth: 0 }}>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {file ? file.name : "選擇 Word 檔（.docx）"}
        </span>
      </label>
    </div>
  );
}

function isDocx(file) {
  return Boolean(file && /\.docx$/i.test(file.name || ""));
}

function notices(diff) {
  const items = [];
  if (diff.summary) items.push(diff.summary);
  for (const warning of diff.warnings || []) {
    if (warning && !items.includes(warning)) items.push(warning);
  }
  if (diff.model_note && !items.includes(diff.model_note)) items.push(diff.model_note);
  return items;
}

function TextBlock({ label, text }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 8 }}>{label}</div>
      <div
        style={{
          margin: "6px 0 0",
          padding: "8px 10px",
          background: "var(--bg-subtle)",
          borderRadius: "var(--radius)",
          whiteSpace: "pre-wrap",
          overflowWrap: "anywhere",
          fontSize: 14,
          lineHeight: 1.65,
        }}
      >
        {text}
      </div>
    </div>
  );
}

function OriginalBlocks({ change }) {
  if (change.kind === "added") {
    return <TextBlock label="新增的條文" text={change.after} />;
  }
  if (change.kind === "removed") {
    return <TextBlock label="原文" text={change.before} />;
  }
  return (
    <>
      <TextBlock label="原文" text={change.before} />
      <TextBlock label="改後" text={change.after} />
    </>
  );
}

function DiffCard({ change, explanation }) {
  const [expanded, setExpanded] = useState(false);
  const important = change.importance !== "low";
  const verb = KIND[change.kind] || "變更";
  return (
    <article
      data-testid="document-diff-card"
      data-diff-id={change.id}
      data-importance={change.importance || "high"}
      style={{
        border: "1px solid var(--border)",
        borderLeft: `3px solid ${change.kind === "removed" ? "var(--danger)" : "var(--accent)"}`,
        borderRadius: "var(--radius)",
        padding: "10px 12px",
        marginTop: 10,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>
          {verb}
          {change.article ? ` · ${change.article}` : ""}
        </h3>
        <span style={{ fontSize: 11, color: "var(--fg-muted)", flexShrink: 0 }}>編號 {change.id}</span>
      </div>
      {change.importance === "low" ? (
        <p style={{ margin: "6px 0 0", fontSize: 13, color: "var(--fg-muted)" }}>只有空白或換行不同</p>
      ) : null}
      {important ? <OriginalBlocks change={change} /> : null}
      {!important && expanded ? <OriginalBlocks change={change} /> : null}
      {!important ? (
        <button type="button" style={{ ...fieldButton, marginTop: 8, minHeight: 32 }} onClick={() => setExpanded((open) => !open)}>
          {expanded ? "收合原文" : "展開原文"}
        </button>
      ) : null}
      {explanation ? (
        <div>
          <div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 8 }}>說明</div>
          <p style={{ margin: "4px 0 0", fontSize: 14, lineHeight: 1.6 }}>{explanation}</p>
        </div>
      ) : null}
    </article>
  );
}

export function DocumentDiffCards({ diff }) {
  if (!diff) return null;
  const explanations = visibleExplanations(diff.changes, diff.explanations);
  const byId = new Map(explanations.map((item) => [item.id, item.text]));
  const alert = !diff.parse_complete || diff.readable === false;
  return (
    <div data-testid="document-diff-cards">
      <div role={alert ? "alert" : "status"}>
        {notices(diff).map((text) => (
          <p key={text} style={{ margin: "0 0 8px", fontSize: 14, lineHeight: 1.55 }}>
            {text}
          </p>
        ))}
      </div>
      {(diff.changes || []).map((change) => (
        <DiffCard key={change.id} change={change} explanation={byId.get(change.id)} />
      ))}
    </div>
  );
}

export function DocumentVersionCompare({
  compareDocuments = compareDocumentVersions,
  explainDocuments = explainDocumentVersions,
}) {
  const [before, setBefore] = useState(null);
  const [after, setAfter] = useState(null);
  const [diff, setDiff] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  function onPick(which, file) {
    const nextBefore = which === "before" ? file : before;
    const nextAfter = which === "after" ? file : after;
    if (which === "before") setBefore(file);
    else setAfter(file);
    setDiff(null);
    const invalid = [nextBefore, nextAfter].some((item) => item && !isDocx(item));
    setError(invalid ? "目前只比對 Word 檔（.docx）。" : "");
  }

  async function run(kind) {
    if (!isDocx(before) || !isDocx(after)) {
      setError("目前只比對 Word 檔（.docx）。");
      return;
    }
    setBusy(kind);
    setError("");
    try {
      const result = kind === "explain"
        ? await explainDocuments(before, after)
        : await compareDocuments(before, after);
      setDiff(result);
    } catch (err) {
      setError(err?.message || "比對失敗");
    } finally {
      setBusy("");
    }
  }

  const ready = isDocx(before) && isDocx(after) && busy === "";
  const canExplain = ready && Boolean(diff?.changes?.length);

  return (
    <div data-testid="document-version-compare">
      <p style={{ margin: "0 0 12px", fontSize: 13, lineHeight: 1.55, color: "var(--fg-muted)" }}>
        {ready
          ? "可以開始比對。清單由程式依條號或大綱產生；模型只說明已經列出的項目。"
          : "選好舊版與新版的 Word（.docx）後才能開始比對。"}
      </p>
      <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
        <FileSlot which="before" label="舊版" file={before} onPick={onPick} />
        <FileSlot which="after" label="新版" file={after} onPick={onPick} />
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
        <button
          type="button"
          disabled={!ready}
          onClick={() => run("compare")}
          style={{ ...primaryButton, opacity: ready ? 1 : 0.5, cursor: ready ? "pointer" : "not-allowed" }}
        >
          {busy === "compare" ? "比對中…" : "開始比對"}
        </button>
        {diff ? (
          <button
            type="button"
            disabled={!canExplain}
            onClick={() => run("explain")}
            style={{ ...fieldButton, opacity: canExplain ? 1 : 0.5, cursor: canExplain ? "pointer" : "not-allowed" }}
          >
            {busy === "explain" ? "說明中…" : "說明差異"}
          </button>
        ) : null}
      </div>
      {error ? (
        <p role="alert" style={{ color: "var(--danger)", fontSize: 13 }}>{error}</p>
      ) : null}
      <DocumentDiffCards diff={diff} />
    </div>
  );
}
