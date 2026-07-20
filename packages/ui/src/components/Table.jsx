// Table — 資料表基礎元件。表頭 muted、列間細邊框、數值/ID 欄可標記 mono
// （doc 12 §3.1：等寬只保留給資料語境）。
import React from "react";

export const Table = ({
  columns = [],
  rows = [],
  rowKey,
  emptyText = "目前沒有資料",
  ...rest
}) => (
  <table
    {...rest}
    style={{
      width: "100%",
      borderCollapse: "collapse",
      fontFamily: "var(--anila-font-sans)",
      fontSize: "var(--anila-text-md)",
      color: "var(--anila-color-fg)",
      ...(rest.style || {}),
    }}
  >
    <thead>
      <tr>
        {columns.map((col) => (
          <th
            key={col.key}
            scope="col"
            style={{
              textAlign: col.align || "left",
              padding: "8px 10px",
              fontSize: "var(--anila-text-sm)",
              fontWeight: "var(--anila-weight-medium)",
              color: "var(--anila-color-fg-muted)",
              borderBottom: "1px solid var(--anila-color-border-strong)",
              whiteSpace: "nowrap",
              width: col.width,
            }}
          >
            {col.title}
          </th>
        ))}
      </tr>
    </thead>
    <tbody>
      {rows.length === 0 ? (
        <tr>
          <td
            colSpan={columns.length || 1}
            style={{
              padding: "18px 10px",
              textAlign: "center",
              color: "var(--anila-color-fg-subtle)",
              fontSize: "var(--anila-text-sm)",
            }}
          >
            {emptyText}
          </td>
        </tr>
      ) : (
        rows.map((row, i) => (
          <tr key={rowKey ? rowKey(row, i) : i}>
            {columns.map((col) => (
              <td
                key={col.key}
                style={{
                  padding: "8px 10px",
                  textAlign: col.align || "left",
                  borderBottom: "1px solid var(--anila-color-border)",
                  fontFamily: col.mono
                    ? "var(--anila-font-mono)"
                    : "inherit",
                  fontVariantNumeric: col.mono ? "tabular-nums" : undefined,
                }}
              >
                {col.render ? col.render(row, i) : row[col.key]}
              </td>
            ))}
          </tr>
        ))
      )}
    </tbody>
  </table>
);
