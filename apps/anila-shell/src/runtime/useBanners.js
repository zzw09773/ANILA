// Admin announcement banners: fetch the active ones after sign-in and
// remember which ones the user dismissed in localStorage.

import { useCallback, useEffect, useState } from "react";
import { listActiveBanners as apiListActiveBanners } from "./conversations.js";

export function useBanners(authRequest, isAuthenticated) {
  const [banners, setBanners] = useState([]);
  const [dismissedBanners, setDismissedBanners] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem("anila-dismissed-banners") || "[]")); }
    catch { return new Set(); }
  });
  useEffect(() => {
    if (!isAuthenticated) return;
    let alive = true;
    apiListActiveBanners(authRequest)
      .then((rows) => { if (alive) setBanners(Array.isArray(rows) ? rows : []); })
      .catch(() => { if (alive) setBanners([]); });
    return () => { alive = false; };
  }, [isAuthenticated, authRequest]);
  const visibleBanners = banners.filter((b) => !dismissedBanners.has(b.id));
  const dismissBanner = useCallback((id) => {
    setDismissedBanners((prev) => {
      const next = new Set(prev); next.add(id);
      try { localStorage.setItem("anila-dismissed-banners", JSON.stringify([...next])); } catch {}
      return next;
    });
  }, []);
  return { visibleBanners, dismissBanner };
}
