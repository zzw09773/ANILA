// Per-user UI settings synced to `users.ui_settings`: sidebar folders, the
// sensitive-info mode (warn / block) and skill auto-apply. The backend is the
// source of truth; localStorage is only this user's offline cache.
//
// Hook order and effects are exactly what ChatRuntime used to run inline.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { getUiSettings, putUiSettings } from "./conversations.js";
import { DEFAULT_FOLDERS } from "../data.jsx";
import { readFoldersCache, resolveFoldersFromServer, writeFoldersCache } from "./folderSettings.js";
import { REDACTION_MODES, REDACTION_MODE_DEFAULT } from "../trust.jsx";

export function useUiSettings({ authRequest, isAuthenticated, user }) {
  // 敏感資訊模式(提醒／阻擋)。使用者自己在提示列選的偏好,跟資料夾
  // 共用 users.ui_settings 這個 per-user blob。
  //
  // ⚠ 存後端而不是 localStorage,理由和資料夾同一條(見下面那段註解),但對這一
  // 項更要緊:卡登共用工作站上,localStorage 會把前一個人的選擇留給下一個人 ——
  // 那不是「使用者的選擇」,而是別人的。blob 跟著卡走才對得起「這是你的偏好」。
  //
  // ⚠ 這**不是**管理員政策。後端沒有這種欄位,這裡也刻意不做成那樣:要不要有
  // 一個管理員層級的強制政策,是還沒裁決的產品問題。
  // 這裡存的只是使用者自己的選擇。
  const [redactionMode, setRedactionMode] = useState(REDACTION_MODE_DEFAULT);


  // folders: persisted locally. Users can add/delete; built-ins (all, starred)
  // are guarded because the sidebar filter logic treats them specially.
  const [folders, setFolders] = useState(DEFAULT_FOLDERS);

  // Server-synced settings:後端是 source of truth(共用工作站下使用者的資料夾
  // 不會殘留在瀏覽器給下一個人看到)。掛載時抓後端覆寫;之後變動 debounce 存回。
  // localStorage 只當這位使用者的離線暫存，key 帶 user id；未區分帳號的舊 key 會被清掉。
  const uiSettingsHydratedRef = useRef(false);
  const [skillAutoApply, setSkillAutoApply] = useState(true);
  const uiSettingsDirtyRef = useRef({ folders: false, redactionMode: false, skillAutoApply: false });
  const uiSettingsSaveErrorRef = useRef(false);
  const foldersRef = useRef(folders);
  const redactionModeRef = useRef(redactionMode);
  const skillAutoApplyRef = useRef(skillAutoApply);
  const [uiSettingsLoadError, setUiSettingsLoadError] = useState(false);
  const [uiSettingsUnsaved, setUiSettingsUnsaved] = useState(false);
  foldersRef.current = folders;
  redactionModeRef.current = redactionMode;
  skillAutoApplyRef.current = skillAutoApply;

  const markUiSettingsUserEdited = useCallback((field) => {
    uiSettingsDirtyRef.current[field] = true;
    setUiSettingsUnsaved(true);
  }, []);

  const changeRedactionMode = useCallback((mode) => {
    markUiSettingsUserEdited("redactionMode");
    setRedactionMode(mode);
  }, [markUiSettingsUserEdited]);

  const persistUiSettings = useCallback((nextFolders, nextRedaction, nextSkillAutoApply = skillAutoApplyRef.current) => {
    return putUiSettings(authRequest, {
      folders: nextFolders,
      redactionMode: nextRedaction,
      skillAutoApply: nextSkillAutoApply !== false,
    })
      .then(() => {
        uiSettingsDirtyRef.current = { folders: false, redactionMode: false, skillAutoApply: false };
        uiSettingsSaveErrorRef.current = false;
        setUiSettingsUnsaved(false);
        setUiSettingsLoadError(false);
      })
      .catch(() => {
        uiSettingsSaveErrorRef.current = true;
        setUiSettingsUnsaved(true);
      });
  }, [authRequest]);

  const applyServerUiSettings = useCallback((s) => {
    const dirty = uiSettingsDirtyRef.current;
    const nextFolders = dirty.folders
      ? foldersRef.current
      : resolveFoldersFromServer(s.folders);
    const nextRedaction = dirty.redactionMode
      ? redactionModeRef.current
      : (REDACTION_MODES.includes(s.redactionMode) ? s.redactionMode : REDACTION_MODE_DEFAULT);
    const nextSkillAutoApply = dirty.skillAutoApply
      ? skillAutoApplyRef.current
      : s.skillAutoApply !== false;
    setFolders(nextFolders);
    setRedactionMode(nextRedaction);
    setSkillAutoApply(nextSkillAutoApply);
    uiSettingsHydratedRef.current = true;
    setUiSettingsLoadError(false);
    if (dirty.folders || dirty.redactionMode || dirty.skillAutoApply) {
      persistUiSettings(nextFolders, nextRedaction, nextSkillAutoApply);
    } else {
      uiSettingsSaveErrorRef.current = false;
      setUiSettingsUnsaved(false);
    }
  }, [persistUiSettings]);

  useEffect(() => {
    if (!isAuthenticated) {
      uiSettingsHydratedRef.current = false;
      uiSettingsDirtyRef.current = { folders: false, redactionMode: false, skillAutoApply: false };
      uiSettingsSaveErrorRef.current = false;
      setFolders(DEFAULT_FOLDERS);
      setRedactionMode(REDACTION_MODE_DEFAULT);
      setSkillAutoApply(true);
      setUiSettingsLoadError(false);
      setUiSettingsUnsaved(false);
      return undefined;
    }
    uiSettingsHydratedRef.current = false;
    setFolders(readFoldersCache(window.localStorage, user?.id));
    setRedactionMode(REDACTION_MODE_DEFAULT);
    let alive = true;
    getUiSettings(authRequest)
      .then((res) => {
        if (!alive) return;
        applyServerUiSettings(res?.ui_settings || {});
      })
      .catch(() => {
        if (!alive) return;
        setUiSettingsLoadError(true);
      });
    return () => { alive = false; };
  }, [isAuthenticated, authRequest, user?.id]);

  const retryUiSettings = useCallback(() => {
    if (!isAuthenticated) return;
    if (uiSettingsSaveErrorRef.current) {
      persistUiSettings(foldersRef.current, redactionModeRef.current);
      return;
    }
    getUiSettings(authRequest)
      .then((res) => {
        applyServerUiSettings(res?.ui_settings || {});
      })
      .catch(() => {
        setUiSettingsLoadError(true);
      });
  }, [isAuthenticated, authRequest, persistUiSettings, applyServerUiSettings]);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    if (!isAuthenticated) return undefined;
    writeFoldersCache(window.localStorage, user?.id, folders);
    if (!uiSettingsHydratedRef.current) return undefined;
    const t = setTimeout(() => {
      persistUiSettings(folders, redactionMode, skillAutoApply);
    }, 600);
    return () => clearTimeout(t);
  }, [folders, redactionMode, skillAutoApply, isAuthenticated, authRequest, user?.id, persistUiSettings]);

  return {
    redactionMode,
    folders,
    setFolders,
    skillAutoApply,
    setSkillAutoApply,
    foldersRef,
    uiSettingsLoadError,
    uiSettingsUnsaved,
    markUiSettingsUserEdited,
    changeRedactionMode,
    retryUiSettings,
  };
}
