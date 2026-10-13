// 交接訪談。題幹只顯示 API 回傳的固定題，不在這裡編，也不在確認前組清單。
import React, { useEffect, useState } from "react";

import { Button, Modal } from "./components.jsx";
import { authRequest as cookieAuthRequest } from "./runtime/api.js";
import {
  confirmHandoverInterview,
  createHandoverInterview,
  formatChecklistText,
  listHandoverInterviews,
  listHandoverQuestions,
  mayRequestChecklist,
  requestChecklist,
  saveHandoverAnswers,
} from "./runtime/handoverInterview.js";

const fieldStyle = {
  width: "100%",
  boxSizing: "border-box",
  padding: "8px 10px",
  fontSize: 13,
  lineHeight: 1.5,
  color: "var(--fg)",
  background: "var(--bg-elev)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
  resize: "vertical",
  fontFamily: "inherit",
};

function viewOf(interview) {
  if (!interview) return "start";
  if (interview.status === "ready") return "done";
  if (interview.status === "confirmed") return "review";
  return "form";
}

function questionList(interview, fallback) {
  const fromInterview = interview?.questions;
  if (Array.isArray(fromInterview) && fromInterview.length) return fromInterview;
  return fallback;
}

export function HandoverInterviewDialog({
  open,
  onClose,
  authRequest = cookieAuthRequest,
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [questions, setQuestions] = useState([]);
  const [interview, setInterview] = useState(null);
  const [recentReady, setRecentReady] = useState(null);
  const [label, setLabel] = useState("");
  const [answers, setAnswers] = useState({});

  useEffect(() => {
    if (!open) return undefined;
    let alive = true;
    setLoading(true);
    setError("");
    setCopied(false);
    setInterview(null);
    setRecentReady(null);
    setLabel("");
    Promise.all([
      listHandoverQuestions(authRequest),
      listHandoverInterviews(authRequest),
    ])
      .then(([pack, list]) => {
        if (!alive) return;
        const rows = Array.isArray(pack?.questions) ? pack.questions : [];
        if (!rows.length) {
          setQuestions([]);
          setError("目前沒有訪談題目，無法開始。");
          return;
        }
        setQuestions(rows);
        const interviews = Array.isArray(list?.interviews) ? list.interviews : [];
        const resume = interviews.find((row) => row.status === "answering" || row.status === "confirmed");
        const ready = interviews.find((row) => row.status === "ready");
        setRecentReady(ready || null);
        if (resume) {
          setInterview(resume);
          setAnswers(resume.answers || {});
          setLabel(resume.project_label || "");
        }
      })
      .catch((err) => {
        if (!alive) return;
        setError(err?.message || "讀取訪談失敗，請再試一次。");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, authRequest]);

  if (!open) return null;

  const view = viewOf(interview);
  const shownQuestions = questionList(interview, questions);
  const showChecklist = view === "done" && interview?.checklist && Array.isArray(interview.checklist.sections);

  const startNew = () => {
    setInterview(null);
    setAnswers({});
    setLabel("");
    setError("");
    setCopied(false);
  };

  const start = async () => {
    setBusy(true);
    setError("");
    try {
      const created = await createHandoverInterview(authRequest, label.trim());
      setInterview(created);
      setAnswers(created.answers || {});
    } catch (err) {
      setError(err?.message || "無法開始訪談");
    } finally {
      setBusy(false);
    }
  };

  const confirmAnswers = async () => {
    if (!interview || interview.status !== "answering") return;
    setBusy(true);
    setError("");
    try {
      const saved = await saveHandoverAnswers(authRequest, interview.id, answers);
      const confirmed = await confirmHandoverInterview(authRequest, saved.id);
      setInterview(confirmed);
      setAnswers(confirmed.answers || answers);
    } catch (err) {
      setError(err?.message || "確認失敗");
    } finally {
      setBusy(false);
    }
  };

  const generate = async () => {
    if (!mayRequestChecklist(interview)) return;
    setBusy(true);
    setError("");
    try {
      const next = await requestChecklist(authRequest, interview);
      setInterview(next);
    } catch (err) {
      setError(err?.message || "無法產生交接清單");
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    const text = formatChecklistText(interview?.checklist);
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setError("無法複製，請自行選取清單文字。");
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="交接訪談"
      subtitle="調動或離開專案前的固定提問。這不是把對話交給同事。"
      width={560}
    >
      <div style={{ display: "grid", gap: 14 }}>
        {loading && (
          <div style={{ fontSize: 13, color: "var(--fg-muted)" }}>正在讀取訪談題目…</div>
        )}

        {!loading && view === "start" && (
          <>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, color: "var(--fg-muted)" }}>
              把沒寫下來的脈絡、踩過的坑、還沒決定的事留成一份清單。
              共 {questions.length} 題，題目固定，不會臨時加題。確認之前不會產生交接清單。
            </p>
            <label style={{ display: "grid", gap: 6 }}>
              <span style={{ fontSize: 12, fontWeight: 500 }}>這次交接的事項（選填）</span>
              <input
                value={label}
                maxLength={80}
                placeholder="例如：週報彙整"
                onChange={(event) => setLabel(event.target.value)}
                style={fieldStyle}
              />
            </label>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
              {recentReady && (
                <Button onClick={() => setInterview(recentReady)} disabled={busy}>
                  查看最近一份交接清單
                </Button>
              )}
              <Button variant="primary" onClick={start} disabled={busy || questions.length === 0}>
                {busy ? "開始中…" : "開始訪談"}
              </Button>
            </div>
          </>
        )}

        {!loading && (view === "form" || view === "review") && (
          <>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, color: "var(--fg-muted)" }}>
              {view === "form"
                ? "題目是固定的，順序也不會改。確認之前不會產生交接清單。"
                : "已確認。交接清單還沒產生。下面是你確認過的回答。"}
            </p>
            {interview?.project_label ? (
              <div style={{ fontSize: 13 }}>事項：{interview.project_label}</div>
            ) : null}
            {shownQuestions.map((question, index) => {
              const value = answers[question.id] || "";
              return (
                <section key={question.id || index} style={{ display: "grid", gap: 6 }}>
                  <div style={{ fontSize: 11, color: "var(--fg-subtle)" }}>
                    第 {index + 1} 題 · {question.heading}
                  </div>
                  <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.5 }}>{question.prompt}</div>
                  {view === "form" ? (
                    <textarea
                      aria-label={question.prompt}
                      value={value}
                      maxLength={2000}
                      rows={3}
                      placeholder={question.placeholder || ""}
                      onChange={(event) => {
                        const next = event.target.value;
                        setAnswers((prev) => ({ ...prev, [question.id]: next }));
                      }}
                      style={fieldStyle}
                    />
                  ) : (
                    <div style={{
                      fontSize: 13,
                      lineHeight: 1.6,
                      whiteSpace: "pre-wrap",
                      padding: "8px 10px",
                      background: "var(--bg-subtle)",
                      border: "1px solid var(--border)",
                      borderRadius: "var(--radius)",
                    }}>
                      {value.trim() ? value : "（尚未填寫）"}
                    </div>
                  )}
                </section>
              );
            })}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
              <Button onClick={startNew} disabled={busy}>開始新的訪談</Button>
              {view === "form" && (
                <Button variant="primary" onClick={confirmAnswers} disabled={busy}>
                  {busy ? "確認中…" : "確認內容無誤"}
                </Button>
              )}
              {view === "review" && mayRequestChecklist(interview) && (
                <Button variant="primary" onClick={generate} disabled={busy}>
                  {busy ? "產生中…" : "產生交接清單"}
                </Button>
              )}
            </div>
          </>
        )}

        {!loading && showChecklist && (
          <div data-testid="handover-checklist" style={{ display: "grid", gap: 12 }}>
            <div style={{ fontSize: 15, fontWeight: 600 }}>交接清單</div>
            {interview.checklist.project_label ? (
              <div style={{ fontSize: 13 }}>事項：{interview.checklist.project_label}</div>
            ) : null}
            {interview.checklist.sections.map((section) => (
              <section key={section.topic || section.heading} style={{ display: "grid", gap: 8 }}>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{section.heading}</div>
                {(section.items || []).map((item) => (
                  <div key={item.question_id || item.prompt} style={{ display: "grid", gap: 4 }}>
                    <div style={{ fontSize: 13 }}>{item.prompt}</div>
                    <div style={{ fontSize: 13, lineHeight: 1.6, color: "var(--fg-muted)", whiteSpace: "pre-wrap" }}>
                      {item.note}
                    </div>
                  </div>
                ))}
              </section>
            ))}
            {interview.checklist.notice ? (
              <div style={{
                fontSize: 12,
                lineHeight: 1.6,
                padding: "8px 10px",
                background: "var(--bg-subtle)",
                border: "1px solid var(--border)",
                borderRadius: "var(--radius)",
              }}>
                {interview.checklist.notice}
              </div>
            ) : null}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
              <Button onClick={startNew}>再做一份</Button>
              <Button variant="primary" onClick={copy}>
                {copied ? "已複製" : "複製清單"}
              </Button>
            </div>
          </div>
        )}

        {error && (
          <div style={{
            padding: "6px 10px",
            background: "oklch(0.97 0.03 25)",
            border: "1px solid oklch(0.88 0.08 25)",
            borderRadius: "var(--radius)",
            color: "var(--danger)",
            fontSize: 12,
          }}>
            {error}
          </div>
        )}
      </div>
    </Modal>
  );
}
