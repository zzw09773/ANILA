// Minimal line icons — 1.5px stroke, no fills, distinct from Lucide/Heroicons
import React from "react";

export const Icon = ({ children, size = 16, stroke = 1.5, className = "" }) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size}
    viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round"
    className={className}>{children}</svg>
);

// ANILA brand glyph: concentric rotated squares — router/convergence metaphor
export const AnilaGlyph = ({ size = 20, className = "" }) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size}
    viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.6" strokeLinejoin="round"
    className={className}>
    <rect x="4" y="4" width="16" height="16" transform="rotate(45 12 12)" />
    <rect x="8" y="8" width="8"  height="8"  transform="rotate(45 12 12)" />
    <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
  </svg>
);

export const IconSend      = (p) => <Icon {...p}><path d="M5 12h14M13 6l6 6-6 6" /></Icon>;
export const IconPlus      = (p) => <Icon {...p}><path d="M12 5v14M5 12h14" /></Icon>;
export const IconSearch    = (p) => <Icon {...p}><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></Icon>;
export const IconSettings  = (p) => <Icon {...p}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></Icon>;
export const IconKey       = (p) => <Icon {...p}><circle cx="7.5" cy="15.5" r="4"/><path d="M10.5 12.5L20 3M16 7l3 3M18 5l3 3"/></Icon>;
export const IconChevDown  = (p) => <Icon {...p}><path d="M6 9l6 6 6-6"/></Icon>;
export const IconChevRight = (p) => <Icon {...p}><path d="M9 6l6 6-6 6"/></Icon>;
export const IconChevUp    = (p) => <Icon {...p}><path d="M18 15l-6-6-6 6"/></Icon>;
export const IconCheck     = (p) => <Icon {...p}><path d="M20 6L9 17l-5-5"/></Icon>;
export const IconX         = (p) => <Icon {...p}><path d="M18 6L6 18M6 6l12 12"/></Icon>;
export const IconPaperclip = (p) => <Icon {...p}><path d="M21 11.5l-8.5 8.5a5 5 0 0 1-7-7L14.5 4.5a3.5 3.5 0 0 1 5 5L11 18a2 2 0 0 1-3-3l7-7"/></Icon>;
export const IconImage     = (p) => <Icon {...p}><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/></Icon>;
export const IconFile      = (p) => <Icon {...p}><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></Icon>;
export const IconMessage   = (p) => <Icon {...p}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></Icon>;
export const IconGrid      = (p) => <Icon {...p}><rect x="3" y="3"  width="7" height="7"/><rect x="14" y="3"  width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></Icon>;
export const IconHistory   = (p) => <Icon {...p}><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></Icon>;
export const IconLogout    = (p) => <Icon {...p}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></Icon>;
export const IconSpark     = (p) => <Icon {...p}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/></Icon>;
export const IconTerminal  = (p) => <Icon {...p}><path d="M4 17l6-6-6-6"/><path d="M12 19h8"/></Icon>;
export const IconPanelR    = (p) => <Icon {...p}><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></Icon>;
export const IconCopy      = (p) => <Icon {...p}><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></Icon>;
export const IconThumbUp   = (p) => <Icon {...p}><path d="M7 22V11M2 13v7a2 2 0 0 0 2 2h3V11H4a2 2 0 0 0-2 2zM15 5.88L14 10h5.83a2 2 0 0 1 2 2.28l-1.17 8A2 2 0 0 1 18.66 22H7V11l3.55-8.11A3 3 0 0 1 13 2h0a2 2 0 0 1 2 2z"/></Icon>;
export const IconThumbDn   = (p) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={p.size||16} height={p.size||16} viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth={p.stroke||1.5} strokeLinecap="round" strokeLinejoin="round"
    style={{transform:"scaleY(-1)"}} className={p.className||""}>
    <path d="M7 22V11M2 13v7a2 2 0 0 0 2 2h3V11H4a2 2 0 0 0-2 2zM15 5.88L14 10h5.83a2 2 0 0 1 2 2.28l-1.17 8A2 2 0 0 1 18.66 22H7V11l3.55-8.11A3 3 0 0 1 13 2h0a2 2 0 0 1 2 2z"/>
  </svg>
);
export const IconRefresh   = (p) => <Icon {...p}><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/><path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14"/></Icon>;
export const IconUser      = (p) => <Icon {...p}><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></Icon>;
export const IconEye       = (p) => <Icon {...p}><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></Icon>;
export const IconEyeOff    = (p) => <Icon {...p}><path d="M17.94 17.94A10.5 10.5 0 0 1 12 20c-7 0-11-8-11-8a21 21 0 0 1 5.17-5.94M9.9 4.24A10.5 10.5 0 0 1 12 4c7 0 11 8 11 8a21 21 0 0 1-3.17 4.42"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></Icon>;
export const IconArrowRight= (p) => <Icon {...p}><path d="M5 12h14M13 6l6 6-6 6"/></Icon>;
export const IconCircleDot = (p) => <Icon {...p}><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2" fill="currentColor" stroke="none"/></Icon>;
export const IconRoute     = (p) => <Icon {...p}><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M8 19h8a4 4 0 0 0 0-8H8a4 4 0 0 1 0-8h8"/></Icon>;
export const IconSun       = (p) => <Icon {...p}><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></Icon>;
export const IconMoon      = (p) => <Icon {...p}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></Icon>;
export const IconBook      = (p) => <Icon {...p}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></Icon>;
export const IconShield    = (p) => <Icon {...p}><path d="M12 2l9 4v6c0 5-3.5 9-9 10-5.5-1-9-5-9-10V6z"/></Icon>;
export const IconLock      = (p) => <Icon {...p}><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></Icon>;
export const IconUnlock    = (p) => <Icon {...p}><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/></Icon>;
export const IconAlert     = (p) => <Icon {...p}><path d="M12 2L1 21h22z"/><path d="M12 9v4M12 17h0"/></Icon>;
export const IconShare     = (p) => <Icon {...p}><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></Icon>;
export const IconTag       = (p) => <Icon {...p}><path d="M20.6 13.4l-7.6 7.6a2 2 0 0 1-2.8 0l-8-8a1 1 0 0 1-.3-.7V4a1 1 0 0 1 1-1h8.3c.3 0 .5.1.7.3l8.7 8.7a2 2 0 0 1 0 2.4z"/><circle cx="7" cy="7" r="1.2" fill="currentColor" stroke="none"/></Icon>;
export const IconFolder    = (p) => <Icon {...p}><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></Icon>;
export const IconStar      = (p) => <Icon {...p}><path d="M12 3l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.9 6.1 21l1.2-6.5L2.5 9.9 9.1 9z"/></Icon>;
export const IconInbox     = (p) => <Icon {...p}><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></Icon>;
export const IconAt        = (p) => <Icon {...p}><circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8"/></Icon>;
export const IconColumns   = (p) => <Icon {...p}><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 3v18"/></Icon>;
export const IconGauge     = (p) => <Icon {...p}><path d="M3 14a9 9 0 1 1 18 0"/><path d="M12 14l4-3"/><circle cx="12" cy="14" r="1.3" fill="currentColor" stroke="none"/></Icon>;
export const IconExternal  = (p) => <Icon {...p}><path d="M15 3h6v6"/><path d="M10 14L21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></Icon>;
export const IconLink      = (p) => <Icon {...p}><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 1 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 1 0 7 7l1-1"/></Icon>;
export const IconNodes     = (p) => <Icon {...p}><circle cx="4" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="20" cy="12" r="2"/><path d="M6 12h4M14 12h4"/></Icon>;
