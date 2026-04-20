import React from "react";

function IconBase({ children, size = 16, stroke = 1.5 }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={stroke}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

export function AnilaGlyph({ size = 20 }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinejoin="round"
    >
      <rect x="4" y="4" width="16" height="16" transform="rotate(45 12 12)" />
      <rect x="8" y="8" width="8" height="8" transform="rotate(45 12 12)" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
    </svg>
  );
}

export const IconArrowRight = (props) => (
  <IconBase {...props}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </IconBase>
);
export const IconBook = (props) => (
  <IconBase {...props}>
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
    <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
  </IconBase>
);
export const IconCheck = (props) => (
  <IconBase {...props}>
    <path d="M20 6L9 17l-5-5" />
  </IconBase>
);
export const IconChevronDown = (props) => (
  <IconBase {...props}>
    <path d="M6 9l6 6 6-6" />
  </IconBase>
);
export const IconChevronRight = (props) => (
  <IconBase {...props}>
    <path d="M9 6l6 6-6 6" />
  </IconBase>
);
export const IconColumns = (props) => (
  <IconBase {...props}>
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <path d="M12 3v18" />
  </IconBase>
);
export const IconCopy = (props) => (
  <IconBase {...props}>
    <rect x="9" y="9" width="12" height="12" rx="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </IconBase>
);
export const IconExternal = (props) => (
  <IconBase {...props}>
    <path d="M15 3h6v6" />
    <path d="M10 14L21 3" />
    <path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" />
  </IconBase>
);
export const IconKey = (props) => (
  <IconBase {...props}>
    <circle cx="7.5" cy="15.5" r="4" />
    <path d="M10.5 12.5L20 3M16 7l3 3M18 5l3 3" />
  </IconBase>
);
export const IconLock = (props) => (
  <IconBase {...props}>
    <rect x="4" y="11" width="16" height="10" rx="2" />
    <path d="M8 11V7a4 4 0 0 1 8 0v4" />
  </IconBase>
);
export const IconLogout = (props) => (
  <IconBase {...props}>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <path d="M16 17l5-5-5-5" />
    <path d="M21 12H9" />
  </IconBase>
);
export const IconMessage = (props) => (
  <IconBase {...props}>
    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
  </IconBase>
);
export const IconNodes = (props) => (
  <IconBase {...props}>
    <circle cx="4" cy="12" r="2" />
    <circle cx="12" cy="12" r="2" />
    <circle cx="20" cy="12" r="2" />
    <path d="M6 12h4M14 12h4" />
  </IconBase>
);
export const IconRoute = (props) => (
  <IconBase {...props}>
    <circle cx="6" cy="19" r="2" />
    <circle cx="18" cy="5" r="2" />
    <path d="M8 19h8a4 4 0 0 0 0-8H8a4 4 0 0 1 0-8h8" />
  </IconBase>
);
export const IconSearch = (props) => (
  <IconBase {...props}>
    <circle cx="11" cy="11" r="7" />
    <path d="M20 20l-3.5-3.5" />
  </IconBase>
);
export const IconSend = (props) => (
  <IconBase {...props}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </IconBase>
);
export const IconSettings = (props) => (
  <IconBase {...props}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
  </IconBase>
);
export const IconUser = (props) => (
  <IconBase {...props}>
    <circle cx="12" cy="8" r="4" />
    <path d="M4 21a8 8 0 0 1 16 0" />
  </IconBase>
);
export const IconX = (props) => (
  <IconBase {...props}>
    <path d="M18 6L6 18M6 6l12 12" />
  </IconBase>
);
