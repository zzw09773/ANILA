import React, { useEffect, useState } from "react";

import { Modal } from "./components.jsx";
import { getMyUsage } from "./runtime/conversations.js";
import {
  DEFAULT_USAGE_RANGE,
  USAGE_RANGES,
  isUsageEmpty,
  usageBodyTokens,
} from "./runtime/usageDisplay.js";

export function ConversationUsageChip({ usage }) {
  if (!usage) return null;
  const total = usageBodyTokens(usage);
  return (
    <details
      data-conversation-usage=""
      style={{ fontSize: 11, color: "var(--fg-muted)", position: "relative" }}
    >
      <summary style={{ cursor: "pointer" }} aria-label="本對話用量">
        本對話 {total} tokens
      </summary>
      <div
        style={{
          position: "absolute",
          right: 0,
          top: "120%",
          zIndex: 5,
          minWidth: 148,
          padding: "8px 10px",
          background: "var(--bg-elev)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          boxShadow: "0 8px 24px -12px oklch(0.10 0 0 / 0.35)",
          fontSize: 11,
          lineHeight: 1.65,
          color: "var(--fg)",
        }}
      >
        <div>輸入 {usage.prompt_tokens ?? 0}</div>
        <div>輸出 {usage.completion_tokens ?? 0}</div>
        <div>思考 {usage.reasoning_tokens ?? 0}</div>
      </div>
    </details>
  );
}

function MetricCard({ id, label, value }) {
  return (
    <div
      data-usage-metric={id}
      data-testid={`usage-metric-${id}`}
      style={{
        padding: "12px 14px",
        background: "var(--bg-subtle)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        minWidth: 0,
      }}
    >
      <div style={{ fontSize: 11, color: "var(--fg-muted)", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 600, fontFamily: "var(--font-mono)", letterSpacing: -0.3 }}>
        {value ?? 0}
      </div>
    </div>
  );
}

function DayBars({ days }) {
  const rows = Array.isArray(days) ? days : [];
  if (!rows.length) return null;
  const totals = rows.map((d) => usageBodyTokens(d));
  const reasonings = rows.map((d) => d.reasoning_tokens || 0);
  const max = Math.max(1, ...totals, ...reasonings);
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>依日</div>
      <div
        role="img"
        aria-label="依日用量"
        style={{ display: "flex", alignItems: "flex-end", gap: 10, height: 96 }}
      >
        {rows.map((d, i) => {
          const total = totals[i];
          const reasoning = reasonings[i];
          const totalH = Math.max(2, Math.round((total / max) * 80));
          const reasonH = reasoning > 0 ? Math.max(2, Math.round((reasoning / max) * 80)) : 0;
          return (
            <div key={d.date || i} style={{ flex: 1, minWidth: 28, textAlign: "center" }}>
              <div
                style={{ display: "flex", alignItems: "flex-end", justifyContent: "center", gap: 3, height: 80 }}
                title={`${d.date}：總數 ${total} · 思考 ${reasoning}（另計）`}
                data-day-total={total}
                data-day-reasoning={reasoning}
              >
                <div
                  aria-hidden="true"
                  style={{
                    width: 8,
                    height: totalH,
                    background: "var(--accent)",
                    borderRadius: 2,
                    opacity: 0.9,
                  }}
                />
                {reasonH ? (
                  <div
                    aria-hidden="true"
                    style={{
                      width: 8,
                      height: reasonH,
                      background: "var(--fg-muted)",
                      borderRadius: 2,
                      opacity: 0.55,
                    }}
                  />
                ) : null}
              </div>
              <div
                style={{
                  marginTop: 4,
                  fontSize: 10,
                  color: "var(--fg-muted)",
                  fontFamily: "var(--font-mono)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {String(d.date || "").slice(5)}
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 8 }}>
        思考 tokens 另計，不含在總數
      </div>
    </div>
  );
}

function SimpleTable({ title, headers, rows, empty }) {
  if (empty) return null;
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>{title}</div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr>
            {headers.map((h) => (
              <th
                key={h}
                style={{
                  textAlign: "left",
                  padding: "4px 6px",
                  color: "var(--fg-muted)",
                  fontWeight: 500,
                  borderBottom: "1px solid var(--border)",
                }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>
  );
}

export function UsagePage({ open, onClose, request }) {
  const [range, setRange] = useState(DEFAULT_USAGE_RANGE);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setLoading(true);
    setError("");
    getMyUsage(request, range)
      .then((row) => {
        if (!cancelled) setData(row);
      })
      .catch((err) => {
        if (!cancelled) {
          setData(null);
          setError(err?.message || "無法載入用量");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, range, request]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="我的用量"
      subtitle="這段期間的請求與 token"
      width={760}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16, maxHeight: "70vh", overflow: "auto" }}>
        <div style={{ display: "flex", gap: 6 }} role="group" aria-label="用量期間">
          {USAGE_RANGES.map((r) => (
            <button
              key={r}
              type="button"
              aria-pressed={range === r}
              onClick={() => setRange(r)}
              style={{
                padding: "4px 10px",
                fontSize: 12,
                fontFamily: "var(--font-mono)",
                background: range === r ? "var(--accent-soft, var(--bg-subtle))" : "var(--bg)",
                border: `1px solid ${range === r ? "var(--accent)" : "var(--border)"}`,
                borderRadius: 999,
                color: "var(--fg)",
                cursor: "pointer",
              }}
            >
              {r}
            </button>
          ))}
        </div>

        {error ? (
          <div role="alert" style={{ fontSize: 13, color: "var(--danger)" }}>{error}</div>
        ) : null}

        {loading && !data && !error ? (
          <div style={{ fontSize: 13, color: "var(--fg-muted)" }}>載入用量中…</div>
        ) : null}

        {data ? (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 8 }}>
              <MetricCard id="requests" label="請求數" value={data.requests} />
              <MetricCard id="prompt_tokens" label="輸入 tokens" value={data.prompt_tokens} />
              <MetricCard id="completion_tokens" label="輸出 tokens" value={data.completion_tokens} />
              <MetricCard id="total_tokens" label="總數" value={usageBodyTokens(data)} />
            </div>
            <div
              data-testid="usage-metric-reasoning_tokens"
              data-usage-metric="reasoning_tokens"
              style={{ fontSize: 12, color: "var(--fg-muted)" }}
            >
              思考 tokens {data.reasoning_tokens ?? 0}（另計，不含在總數）
            </div>

            {isUsageEmpty(data) ? (
              <div style={{ fontSize: 13, color: "var(--fg-muted)" }}>這段期間沒有用量</div>
            ) : (
              <>
                <SimpleTable
                  title="依模型"
                  headers={["模型", "請求", "輸入", "輸出", "思考"]}
                  empty={!(data.by_model || []).length}
                  rows={(data.by_model || []).map((row) => (
                    <tr key={row.model_id ?? row.model_name}>
                      <td style={{ padding: "6px" }}>{row.model_name || row.model_id}</td>
                      <td style={{ padding: "6px", fontFamily: "var(--font-mono)" }}>{row.requests ?? 0}</td>
                      <td style={{ padding: "6px", fontFamily: "var(--font-mono)" }}>{row.prompt_tokens ?? 0}</td>
                      <td style={{ padding: "6px", fontFamily: "var(--font-mono)" }}>{row.completion_tokens ?? 0}</td>
                      <td style={{ padding: "6px", fontFamily: "var(--font-mono)" }}>{row.reasoning_tokens ?? 0}</td>
                    </tr>
                  ))}
                />
                <DayBars days={data.by_day} />
                <SimpleTable
                  title="依類型"
                  headers={["類型", "請求", "合計 tokens"]}
                  empty={!(data.by_kind || []).length}
                  rows={(data.by_kind || []).map((row) => (
                    <tr key={row.kind}>
                      <td style={{ padding: "6px" }}>{row.kind}</td>
                      <td style={{ padding: "6px", fontFamily: "var(--font-mono)" }}>{row.requests ?? 0}</td>
                      <td style={{ padding: "6px", fontFamily: "var(--font-mono)" }}>{row.total_tokens ?? 0}</td>
                    </tr>
                  ))}
                />
              </>
            )}
          </>
        ) : null}
      </div>
    </Modal>
  );
}
