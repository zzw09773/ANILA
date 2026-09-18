// 共用工作站上，資料夾名稱必須跟著登入帳號走，不能寫進未區分帳號的 localStorage。

import { DEFAULT_FOLDERS } from "../data.jsx";

export const LEGACY_FOLDERS_STORAGE_KEY = "anila-folders";
export const FOLDERS_STORAGE_PREFIX = "anila-folders:";

export function foldersStorageKey(userId) {
  if (userId == null || userId === "") return null;
  return `${FOLDERS_STORAGE_PREFIX}${userId}`;
}

export function parseFolderList(value) {
  if (!Array.isArray(value) || value.length === 0) return null;
  const cleaned = value.filter(
    (f) => f && typeof f.id === "string" && typeof f.name === "string",
  );
  return cleaned.length > 0 ? cleaned : null;
}

/** 後端沒有資料夾（新帳號或讀取失敗）時，明確回到預設，不要留前一人的名稱。 */
export function resolveFoldersFromServer(serverFolders) {
  return parseFolderList(serverFolders) || DEFAULT_FOLDERS;
}

export function readFoldersCache(storage, userId) {
  const key = foldersStorageKey(userId);
  if (!key || !storage) return DEFAULT_FOLDERS;
  try {
    const raw = storage.getItem(key);
    if (!raw) return DEFAULT_FOLDERS;
    return parseFolderList(JSON.parse(raw)) || DEFAULT_FOLDERS;
  } catch {
    return DEFAULT_FOLDERS;
  }
}

export function writeFoldersCache(storage, userId, folders) {
  if (!storage) return;
  const key = foldersStorageKey(userId);
  if (!key) return;
  try {
    storage.setItem(key, JSON.stringify(folders));
    storage.removeItem(LEGACY_FOLDERS_STORAGE_KEY);
  } catch {
    /* quota / private mode */
  }
}

export function clearFoldersSession(storage, userId) {
  if (!storage) return;
  try {
    storage.removeItem(LEGACY_FOLDERS_STORAGE_KEY);
    const key = foldersStorageKey(userId);
    if (key) storage.removeItem(key);
  } catch {
    /* ignore */
  }
}
