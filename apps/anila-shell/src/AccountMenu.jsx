import React, { useEffect, useRef, useState } from "react";

import { ANILA_VERSION, isGovernanceRole } from "../../shared/product.js";
import { governanceHref } from "./appOrigins.js";

export function AccountMenu({ user, onLogout, onOpenSettings }) {
  const [open, setOpen] = useState(false);
  const root = useRef(null);

  useEffect(() => {
    function onDoc(e) {
      if (root.current && !root.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("click", onDoc);
    return () => document.removeEventListener("click", onDoc);
  }, []);

  return (
    <div className="anila-topbar__account" ref={root}>
      <button
        type="button"
        className="anila-topbar__who"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((v) => !v)}
      >
        {user?.username || "未登入"}
      </button>
      {open ? (
        <div className="anila-topbar__menu" role="menu">
          <span className="anila-topbar__ver">版本 {ANILA_VERSION}</span>
          {isGovernanceRole(user?.role) ? (
            <a href={governanceHref("/")} role="menuitem" onClick={() => setOpen(false)}>
              系統管理
            </a>
          ) : null}
          {typeof onOpenSettings === "function" ? (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onOpenSettings();
              }}
            >
              設定
            </button>
          ) : null}
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onLogout?.();
            }}
          >
            登出
          </button>
        </div>
      ) : null}
    </div>
  );
}
