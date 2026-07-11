// @anila/ui 內部圖示 — 全部 inline SVG（1.5px 線性、24 viewBox），
// 與 shell 的 icons.jsx 同語彙；套件自給自足，零外部 icon 依賴。
import React from "react";

export const Icon = ({
  children,
  size = 16,
  stroke = 1.5,
  className = "",
  "aria-hidden": ariaHidden,
  ...rest
}) => (
  <svg
    {...rest}
    xmlns="http://www.w3.org/2000/svg"
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={stroke}
    strokeLinecap="round"
    strokeLinejoin="round"
    className={className}
    aria-hidden={ariaHidden ?? (rest["aria-label"] ? undefined : true)}
  >
    {children}
  </svg>
);

export const IconX = (p) => (
  <Icon {...p}>
    <path d="M18 6L6 18M6 6l12 12" />
  </Icon>
);
export const IconChevDown = (p) => (
  <Icon {...p}>
    <path d="M6 9l6 6 6-6" />
  </Icon>
);
export const IconCheck = (p) => (
  <Icon {...p}>
    <path d="M20 6L9 17l-5-5" />
  </Icon>
);
export const IconAlert = (p) => (
  <Icon {...p}>
    <path d="M12 2L1 21h22z" />
    <path d="M12 9v4M12 17h0" />
  </Icon>
);
export const IconInfo = (p) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 8h0M12 11v5" />
  </Icon>
);
