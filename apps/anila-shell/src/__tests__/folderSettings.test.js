import { describe, it, expect, beforeEach } from "vitest";

import { DEFAULT_FOLDERS } from "../data.jsx";
import {
  LEGACY_FOLDERS_STORAGE_KEY,
  foldersStorageKey,
  readFoldersCache,
  resolveFoldersFromServer,
  writeFoldersCache,
} from "../runtime/folderSettings.js";

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
    _data: data,
  };
}

const customA = [
  ...DEFAULT_FOLDERS,
  { id: "hr", name: "人資專用", icon: "folder" },
];

describe("folderSettings — 共用工作站隔離", () => {
  let storage;
  beforeEach(() => {
    storage = memoryStorage();
  });

  it("後端空陣列明確回到預設，不沿用呼叫端手上的舊名單", () => {
    expect(resolveFoldersFromServer([])).toEqual(DEFAULT_FOLDERS);
    expect(resolveFoldersFromServer(null)).toEqual(DEFAULT_FOLDERS);
    expect(resolveFoldersFromServer(undefined)).toEqual(DEFAULT_FOLDERS);
  });

  it("A 建的資料夾不會寫進未區分帳號的 key，B 讀自己的快取是預設", () => {
    writeFoldersCache(storage, 1, customA);
    expect(storage.getItem(LEGACY_FOLDERS_STORAGE_KEY)).toBeNull();
    expect(readFoldersCache(storage, 1)).toEqual(customA);
    expect(readFoldersCache(storage, 2)).toEqual(DEFAULT_FOLDERS);
  });

  it("寫入時清掉舊的 anila-folders，避免下一個人讀到前一人的名稱", () => {
    storage.setItem(LEGACY_FOLDERS_STORAGE_KEY, JSON.stringify(customA));
    writeFoldersCache(storage, 2, DEFAULT_FOLDERS);
    expect(storage.getItem(LEGACY_FOLDERS_STORAGE_KEY)).toBeNull();
    expect(readFoldersCache(storage, 2)).toEqual(DEFAULT_FOLDERS);
  });

  it("設定 API 失敗時只回這位使用者的快取，沒有快取就預設", () => {
    writeFoldersCache(storage, 1, customA);
    expect(readFoldersCache(storage, 1)).toEqual(customA);
    expect(readFoldersCache(storage, 99)).toEqual(DEFAULT_FOLDERS);
  });

  it("foldersStorageKey 沒有 user id 就不寫，避免再造未區分帳號的 key", () => {
    expect(foldersStorageKey(null)).toBeNull();
    writeFoldersCache(storage, null, customA);
    expect(storage._data.size).toBe(0);
  });
});
