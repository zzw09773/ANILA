import React, { useEffect, useMemo, useRef, useState } from "react";
import { Button, Modal } from "./components.jsx";
import { compareLetterRewrite } from "./runtime/letterFacts.js";
import {
  LETTER_EXAMPLE,
  LETTER_TONES,
  requestLetterRewrite,
} from "./runtime/letterRewriteRequest.js";

const columnStyle = {
  minWidth: 0,
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
  background: "var(--bg)",
  padding: 12,
};

const bodyStyle = {
  margin: 0,
  whiteSpace: "pre-wrap",
  overflowWrap: "anywhere",
  fontSize: 14,
  lineHeight: 1.7,
  fontFamily: "inherit",
  color: "var(--fg)",
};

function FactMark({ kind, children }) {
  return (
    <mark
      data-letter-fact={kind}
      data-letter-changed="1"
      style={{
        background: "oklch(0.55 0.18 25 / 0.14)",
        color: "var(--danger)",
        fontWeight: 650,
        textDecoration: "underline",
        textDecorationColor: "var(--danger)",
      }}
    >
      {children}
    </mark>
  );
}

export function LetterCompare({ original, rewrite }) {
  const compared = useMemo(
    () => compareLetterRewrite(original, rewrite),
    [original, rewrite],
  );
  const flagged = compared.changed.length > 0;
  return (
    <div data-testid="letter-compare">
      <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--fg-muted)", lineHeight: 1.6 }}>
        紅字由程式比對原文而來，不是模型自己標的。
        {flagged
          ? "請再核對紅字。這些日期、金額或期限和原文不一樣，或是原文裡沒有。"
          : "這次改寫沒有改動日期、金額或期限。"}
      </p>
      {compared.missingLabels.length > 0 ? (
        <p style={{ margin: "0 0 8px", fontSize: 12, lineHeight: 1.6 }} data-testid="letter-missing">
          原文有、改寫沒有：
          {compared.missingLabels.map((label, index) => (
            <React.Fragment key={`${label}-${index}`}>
              {index > 0 ? "、" : ""}
              <FactMark kind="missing">{label}</FactMark>
            </React.Fragment>
          ))}
        </p>
      ) : null}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
        gap: 12,
      }}>
        <section style={columnStyle} aria-label="原文">
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>原文</div>
          <div style={bodyStyle}>{original}</div>
        </section>
        <section style={columnStyle} aria-label="改寫" data-testid="letter-rewrite-column">
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>改寫</div>
          <div style={bodyStyle}>
            {compared.segments.map((segment, index) => (
              segment.flag
                ? <FactMark key={`${segment.flag}-${index}`} kind={segment.flag}>{segment.text}</FactMark>
                : <span key={`plain-${index}`}>{segment.text}</span>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

export function LetterRewritePanel({
  open,
  onClose,
  rewriteRequest = requestLetterRewrite,
}) {
  const [draft, setDraft] = useState("");
  const [sentText, setSentText] = useState("");
  const [rewrite, setRewrite] = useState("");
  const [busyTone, setBusyTone] = useState("");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const abortRef = useRef(null);

  useEffect(() => {
    if (open) return undefined;
    abortRef.current?.abort();
    return undefined;
  }, [open]);

  async function run(tone) {
    const source = draft.trim();
    if (!source || busyTone) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusyTone(tone);
    setError("");
    setCopied(false);
    try {
      const text = await rewriteRequest({
        original: source,
        tone,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setSentText(source);
      setRewrite(text);
    } catch (err) {
      if (controller.signal.aborted || err?.name === "AbortError") return;
      setRewrite("");
      setSentText("");
      setError(err?.message || "這次改寫沒有完成。請再試一次。");
    } finally {
      if (abortRef.current === controller) setBusyTone("");
    }
  }

  async function copyRewrite() {
    try {
      await navigator.clipboard.writeText(rewrite);
      setCopied(true);
    } catch {
      setError("無法複製。請自行選取改寫文字。");
    }
  }

  const stale = Boolean(rewrite) && draft.trim() !== sentText;

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={920}
      title="改寫信件與公文"
      subtitle="一鍵改成更正式、更和緩或更精簡。範例是虛構的，請換成你要改的文字。"
    >
      <label style={{ display: "block" }}>
        <span style={{ display: "block", fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
          信件或公文
        </span>
        <textarea
          aria-label="信件或公文原文"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="貼上要改寫的信件或公文"
          rows={8}
          style={{
            width: "100%",
            boxSizing: "border-box",
            resize: "vertical",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            background: "var(--bg)",
            color: "var(--fg)",
            padding: 10,
            font: "inherit",
            lineHeight: 1.6,
          }}
        />
      </label>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10 }}>
        <Button
          type="button"
          variant="subtle"
          onClick={() => {
            setDraft(LETTER_EXAMPLE);
            setRewrite("");
            setSentText("");
            setError("");
            setCopied(false);
          }}
        >
          填入範例
        </Button>
        {LETTER_TONES.map((tone) => (
          <Button
            key={tone.id}
            type="button"
            variant="primary"
            disabled={!draft.trim() || Boolean(busyTone)}
            aria-busy={busyTone === tone.id}
            onClick={() => run(tone.id)}
          >
            {tone.label}
          </Button>
        ))}
      </div>
      {busyTone ? (
        <p role="status" style={{ margin: "10px 0 0", fontSize: 12, color: "var(--fg-muted)" }}>
          正在改寫，完成後會並排顯示。
        </p>
      ) : null}
      {error ? (
        <p role="alert" style={{ margin: "10px 0 0", fontSize: 13, color: "var(--danger)" }}>{error}</p>
      ) : null}
      {stale ? (
        <p style={{ margin: "10px 0 0", fontSize: 12, color: "var(--fg-muted)" }}>
          下面左邊是剛剛送出的原文。輸入框已改過的話，請再按一次。
        </p>
      ) : null}
      {rewrite ? (
        <div style={{ marginTop: 14 }}>
          <LetterCompare original={sentText} rewrite={rewrite} />
          <div style={{ marginTop: 10 }}>
            <Button type="button" variant="subtle" onClick={copyRewrite}>
              {copied ? "已複製" : "複製改寫"}
            </Button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
