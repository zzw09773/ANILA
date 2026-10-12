// 回饋結案通知。只顯示這位登入者自己的未讀 feedback_resolved。
// 後端已依使用者過濾；這裡再擋掉其他類型，避免交接或系統通知混進來。
import React, { useEffect, useState } from "react";

import { listNotifications, markNotificationRead } from "./runtime/conversations.js";

export function feedbackResolutionNotices(rows) {
  if (!Array.isArray(rows)) return [];
  return rows.filter((row) => row && row.type === "feedback_resolved" && row.is_read !== true);
}

export function FeedbackResolutionNotice({ authRequest }) {
  const [rows, setRows] = useState([]);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (typeof authRequest !== "function") {
      setRows([]);
      return undefined;
    }
    let alive = true;
    listNotifications(authRequest, { unreadOnly: true })
      .then((list) => {
        if (alive) setRows(feedbackResolutionNotices(list));
      })
      .catch(() => {
        if (alive) setRows([]);
      });
    return () => {
      alive = false;
    };
  }, [authRequest, tick]);

  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), 60000);
    return () => clearInterval(timer);
  }, []);

  const dismiss = async (row) => {
    try {
      await markNotificationRead(authRequest, row.id);
    } catch {
      // 沒標成就先從這一輪拿掉，下一輪再撈。
    }
    setRows((prev) => prev.filter((item) => item.id !== row.id));
  };

  if (rows.length === 0) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {rows.map((row) => (
        <div
          key={row.id}
          data-testid="feedback-resolution-notice"
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 10,
            padding: "8px 16px",
            background: "var(--accent-soft)",
            borderBottom: "1px solid var(--accent)",
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          <span style={{ flex: 1, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            <b>{row.title}</b>
            {row.body ? ` ${row.body}` : ""}
          </span>
          <button
            type="button"
            onClick={() => dismiss(row)}
            style={{
              background: "transparent",
              border: "1px solid var(--border)",
              cursor: "pointer",
              fontSize: 12,
              padding: "2px 8px",
              flexShrink: 0,
            }}
          >知道了</button>
        </div>
      ))}
    </div>
  );
}
