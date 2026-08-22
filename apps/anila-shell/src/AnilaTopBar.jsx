import React from "react";

import { ANILA_LOCKUP, ANILA_LOCKUP_BRAND, ANILA_LOCKUP_LINE } from "../../shared/product.js";
import { shellWorkbenchHref } from "./appOrigins.js";
import { AccountMenu } from "./AccountMenu.jsx";

export function AnilaTopBar({ user, onLogout, onOpenSettings }) {
  return (
    <header className="anila-topbar">
      <a className="anila-lockup" href={shellWorkbenchHref()} aria-label={ANILA_LOCKUP}>
        <span className="anila-lockup__mark" aria-hidden="true" />
        <span className="anila-lockup__word">
          <span className="anila-lockup__brand">{ANILA_LOCKUP_BRAND}</span>
          <span className="anila-lockup__sep">·</span>
          <span className="anila-lockup__line">{ANILA_LOCKUP_LINE}</span>
        </span>
      </a>
      <AccountMenu user={user} onLogout={onLogout} onOpenSettings={onOpenSettings} />
    </header>
  );
}
