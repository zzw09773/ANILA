import React, { useEffect, useId, useRef, useState } from "react";
import { usageCalendarDays, usageLinePoints } from "../runtime/usageDisplay.js";

const MODES = { daily: "每日", weekly: "每週", cumulative: "期間累計" };
const number = (value) => value.toLocaleString("zh-TW");
const label = (point) => `${point.date}${point.end && point.end !== point.date ? ` ～ ${point.end}` : ""}：${number(point.total)} tokens · 思考 ${number(point.reasoning)}（另計）${point.partial ? "（含部分日）" : ""}`;
const heatColor = (level) => level === 0 ? "var(--bg-subtle)" : `color-mix(in oklch, var(--accent) ${[0, 25, 45, 70, 100][level]}%, var(--bg-elev))`;

export default function UsageCharts({ rows, range, requestedAt }) {
  const [mode, setMode] = useState("daily");
  const [detail, setDetail] = useState(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const dayRefs = useRef([]);
  const keyboardHelpId = useId();
  useEffect(() => {
    setActiveIndex(0);
    setDetail(null);
  }, [range, requestedAt]);
  const days = usageCalendarDays(rows, range, requestedAt);
  const points = usageLinePoints(days, mode);
  const max = Math.max(1, ...points.map((point) => point.total));
  const heatMax = Math.max(1, ...days.map((day) => day.total));
  const coordinates = points.map((point, i) => ({
    ...point, x: 44 + i * 572 / Math.max(1, points.length - 1), y: 150 - point.total / max * 124,
  }));
  const offset = (new Date(`${days[0].date}T00:00:00Z`).getUTCDay() + 6) % 7;
  function moveDay(event, index) {
    const row = (index + offset) % 7;
    let next = index;
    switch (event.key) {
      case "ArrowUp": if (row > 0) next--; break;
      case "ArrowDown": if (row < 6) next++; break;
      case "ArrowLeft": next -= 7; break;
      case "ArrowRight": next += 7; break;
      case "Home": next = 0; break;
      case "End": next = days.length - 1; break;
      default: return;
    }
    event.preventDefault();
    if (next >= 0 && next < days.length) {
      setActiveIndex(next);
      dayRefs.current[next]?.focus();
    }
  }
  const weeks = Math.ceil((offset + days.length) / 7);
  const selected = detail || label(points.at(-1));
  return (
    <section aria-label="Token 用量圖表" style={{ minWidth: 0 }}>
      <div role="group" aria-label="折線圖彙整方式" style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {Object.entries(MODES).map(([key, title]) => (
          <button type="button" key={key} aria-pressed={mode === key} onClick={() => { setMode(key); setDetail(null); }} style={{
            padding: "6px 10px", borderRadius: "var(--radius)", border: "1px solid var(--border)",
            color: mode === key ? "var(--accent)" : "var(--fg)", background: mode === key ? "var(--accent-soft)" : "var(--bg-elev)", cursor: "pointer",
          }}>{title}</button>
        ))}
      </div>
      <p style={{ fontSize: 12, color: "var(--fg-muted)" }}>總數＝輸入＋輸出。思考 tokens 另計，不含在總數</p>
      <svg viewBox="0 0 640 190" role="img" aria-label={`${MODES[mode]} Token 折線圖，僅所選期間`} style={{ width: "100%", height: "auto", display: "block", color: "var(--fg-muted)", overflow: "visible" }}>
        <title>{MODES[mode]} Token 用量</title>
        <desc>台北時區。起迄日可能只有部分日；每日資料與圖表數值亦可在下方明細讀取。</desc>
        {[0, 0.5, 1].map((fraction) => <g key={fraction}>
          <line x1="44" x2="616" y1={150 - fraction * 124} y2={150 - fraction * 124} stroke="var(--border)" />
          <text x="40" y={154 - fraction * 124} textAnchor="end" fill="currentColor" fontSize="10">{Intl.NumberFormat("zh-TW", { notation: "compact" }).format(max * fraction)}</text>
        </g>)}
        <polyline data-testid="usage-line" data-values={points.map((point) => point.total).join(",")} points={coordinates.map((point) => `${point.x},${point.y}`).join(" ")} fill="none" stroke="var(--accent)" strokeWidth="2.5" />
        {coordinates.map((point) => <circle key={point.date} cx={point.x} cy={point.y} r="4" fill="var(--accent)"><title>{label(point)}</title></circle>)}
        <text x="44" y="177" fill="currentColor" fontSize="12">{days[0].date.slice(5)}</text>
        <text x="616" y="177" textAnchor="end" fill="currentColor" fontSize="12">{days.at(-1).date.slice(5)}</text>
      </svg>
      <h3 style={{ fontSize: 13, margin: "12px 0 8px" }}>Token 日曆</h3>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "grid", gridTemplateColumns: `repeat(${weeks}, 24px)`, gap: 4 }}>
          {Array.from({ length: weeks }, (_, week) => {
            const monthStart = days.slice(Math.max(0, week * 7 - offset), (week + 1) * 7 - offset)
              .find((day) => day === days[0] || day.date.endsWith("-01"));
            return <span key={week} style={{ gridColumn: week + 1, gridRow: 1, fontSize: 10, color: "var(--fg-muted)", whiteSpace: "nowrap" }}>{monthStart ? `${Number(monthStart.date.slice(5, 7))}月` : ""}</span>;
          })}
          {days.map((day, i) => {
            const level = day.total === 0 ? 0 : Math.max(1, Math.ceil(day.total / heatMax * 4));
            return <button type="button" key={day.date} aria-label={label(day)} title={label(day)}
              ref={(node) => { dayRefs.current[i] = node; }}
              tabIndex={i === Math.min(activeIndex, days.length - 1) ? 0 : -1}
              aria-describedby={keyboardHelpId}
              data-usage-date={day.date} data-day-total={day.total} data-day-reasoning={day.reasoning} data-partial={day.partial}
              onKeyDown={(event) => moveDay(event, i)}
              onMouseEnter={() => setDetail(label(day))}
              onFocus={() => { setActiveIndex(i); setDetail(label(day)); }}
              onClick={() => { setActiveIndex(i); setDetail(label(day)); dayRefs.current[i]?.focus(); }}
              style={{ gridColumn: Math.floor((i + offset) / 7) + 1, gridRow: (i + offset) % 7 + 2,
                width: 24, height: 24, padding: 0, borderRadius: 4, border: "1px solid var(--border)", background: heatColor(level), cursor: "pointer" }} />;
          })}
        </div>
        <div style={{ alignSelf: "end", fontSize: 11, color: "var(--fg-muted)" }}>
          <div aria-label="日曆色階：低到高" style={{ display: "flex", alignItems: "center", gap: 4 }}>
            低{[0, 1, 2, 3, 4].map((level) => <span key={level} aria-hidden="true" style={{ width: 12, height: 12, border: "1px solid var(--border)", borderRadius: 3, background: heatColor(level) }} />)}高
          </div>
          <p>由上到下：週一～週日</p>
        </div>
      </div>
      <p id={keyboardHelpId} style={{ fontSize: 11, color: "var(--fg-muted)" }}>日曆鍵盤操作：↑↓ 同週前後一天，←→ 前後一週；Home／End 到首末日期，Tab 離開日曆。</p>
      <p role="status" style={{ fontSize: 12, minHeight: "2.8em", overflowWrap: "anywhere" }}>{selected}</p>
      <details>
        <summary style={{ cursor: "pointer", fontSize: 12 }}>查看{MODES[mode]}數值明細</summary>
        <ul style={{ paddingLeft: 20, fontSize: 12 }}>{points.map((point) => <li key={point.date}>{label(point)}</li>)}</ul>
      </details>
    </section>
  );
}
