import React, { useEffect, useRef, useState } from "react";
import { useAuth } from "../runtime/auth.jsx";
import {
  isAnswerNotification,
  listNotifications,
  markNotificationRead,
} from "../runtime/notifications.js";

const POLL_MS = 15000;

function conversationIdOf(row) {
  const id = row?.payload?.conversation_id;
  return typeof id === "number" ? id : null;
}

export function AnswerReadyNotice() {
  const { authRequest, user } = useAuth();
  const [rows, setRows] = useState([]);
  const [tick, setTick] = useState(0);
  const seen = useRef(new Set());

  useEffect(() => {
    if (typeof authRequest !== "function" || typeof user?.id !== "number") {
      setRows([]);
      return undefined;
    }
    let alive = true;
    listNotifications(authRequest, { unreadOnly: true })
      .then((list) => {
        if (!alive) return;
        const next = (Array.isArray(list) ? list : []).filter(isAnswerNotification);
        setRows(next);
      })
      .catch(() => {
        if (alive) setRows([]);
      });
    return () => {
      alive = false;
    };
  }, [authRequest, user?.id, tick]);

  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    for (const row of rows) {
      if (seen.current.has(row.id)) continue;
      seen.current.add(row.id);
      const conversationId = conversationIdOf(row);
      if (conversationId == null) continue;
      window.dispatchEvent(new CustomEvent("anila-answer-ready", {
        detail: { conversationId },
      }));
    }
  }, [rows]);

  if (rows.length === 0) return null;

  const open = async (row) => {
    try {
      await markNotificationRead(authRequest, row.id);
    } catch {
      /* 打不開已讀也不擋住查看 */
    }
    setRows((prev) => prev.filter((item) => item.id !== row.id));
    const conversationId = conversationIdOf(row);
    if (conversationId == null) return;
    window.dispatchEvent(new CustomEvent("anila-open-conversation", {
      detail: { conversationId },
    }));
  };

  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {rows.map((row) => (
        <div
          key={row.id}
          role="status"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            flexWrap: "wrap",
            padding: "8px 16px",
            background: row.type === "answer_failed"
              ? "oklch(0.95 0.05 25)"
              : "oklch(0.95 0.05 150)",
            borderBottom: "1px solid " + (
              row.type === "answer_failed" ? "var(--danger)" : "var(--success)"
            ),
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          <span style={{ flex: 1, minWidth: 200 }}>
            <b>{row.title}</b>
            {row.body ? ` ${row.body}` : ""}
          </span>
          <button
            type="button"
            onClick={() => open(row)}
            style={{
              fontSize: 12,
              padding: "4px 10px",
              borderRadius: 999,
              border: "1px solid var(--border)",
              background: "var(--bg-elev)",
              color: "var(--fg)",
              cursor: "pointer",
            }}
          >
            查看
          </button>
        </div>
      ))}
    </div>
  );
}
