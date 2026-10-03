// 對話與摘要的批次操作。每一筆自己成功才算數，失敗不把整份舊清單蓋回去。

import { BUILTIN_FOLDER_IDS } from "../data.jsx";

export const BULK_CONCURRENCY = 4;
export const SUMMARY_BULK_MAX = 200;

export function streamingConversationIds(messagesByConv) {
  const ids = new Set();
  if (!messagesByConv || typeof messagesByConv !== "object") return ids;
  for (const [key, msgs] of Object.entries(messagesByConv)) {
    if (!Array.isArray(msgs) || !msgs.some((m) => m && m.streaming)) continue;
    const n = Number(key);
    ids.add(Number.isInteger(n) && String(n) === key ? n : key);
  }
  return ids;
}

export async function mapLimited(items, worker, limit = BULK_CONCURRENCY) {
  const list = Array.from(items || []);
  const cap = Math.max(1, Number(limit) || 1);
  const results = new Array(list.length);
  let cursor = 0;

  async function run() {
    while (cursor < list.length) {
      const index = cursor;
      cursor += 1;
      const item = list[index];
      try {
        await worker(item, index);
        results[index] = { ok: true, item };
      } catch (error) {
        results[index] = { ok: false, item, error };
      }
    }
  }

  const workers = Math.min(cap, list.length);
  if (workers > 0) {
    await Promise.all(Array.from({ length: workers }, () => run()));
  }
  return {
    succeeded: results.filter((row) => row && row.ok).map((row) => row.item),
    failed: results.filter((row) => row && !row.ok).map((row) => ({ item: row.item, error: row.error })),
  };
}

export function chunkPositiveIds(ids, size = SUMMARY_BULK_MAX) {
  const limit = Math.max(1, Number(size) || SUMMARY_BULK_MAX);
  const seen = new Set();
  const clean = [];
  for (const raw of ids || []) {
    let n = NaN;
    if (typeof raw === "number" && Number.isInteger(raw)) n = raw;
    else if (typeof raw === "string" && /^\d+$/.test(raw)) n = Number(raw);
    if (!Number.isInteger(n) || n <= 0 || seen.has(n)) continue;
    seen.add(n);
    clean.push(n);
  }
  const chunks = [];
  for (let i = 0; i < clean.length; i += limit) chunks.push(clean.slice(i, i + limit));
  return chunks;
}

export async function deleteSummaryBatches(ids, deleteBatch, size = SUMMARY_BULK_MAX) {
  const chunks = chunkPositiveIds(ids, size);
  const succeeded = [];
  const failed = [];
  for (const chunk of chunks) {
    try {
      const res = await deleteBatch(chunk);
      const deleted = res && typeof res.deleted === "number" ? res.deleted : NaN;
      if (deleted !== chunk.length) {
        failed.push(...chunk);
        continue;
      }
      succeeded.push(...chunk);
    } catch {
      failed.push(...chunk);
    }
  }
  return { succeeded, failed, batches: chunks.length };
}

export function moveFolderOptions(folders) {
  const options = [];
  for (const folder of folders || []) {
    if (!folder || typeof folder.id !== "string" || typeof folder.name !== "string") continue;
    if (BUILTIN_FOLDER_IDS.has(folder.id) || folder.id === "all" || folder.id === "starred") continue;
    options.push({ value: folder.id, label: folder.name });
  }
  options.push({ value: "all", label: "取消群組" });
  return options;
}

export function appendRemoteHits(conversations, filtered, serverHits) {
  const localIds = new Set((filtered || []).map((row) => row && row.id));
  const known = new Set((conversations || []).map((row) => row && row.id));
  const extras = [];
  const seen = new Set();
  for (const hit of serverHits || []) {
    if (!hit || hit.id == null || localIds.has(hit.id) || known.has(hit.id) || seen.has(hit.id)) continue;
    seen.add(hit.id);
    extras.push({
      id: hit.id,
      title: hit.title,
      agentId: hit.agent_id ?? hit.agentId ?? null,
      updatedAt: hit.updated_at || hit.updatedAt,
      createdAt: hit.created_at || hit.createdAt,
      classified: hit.classified,
      snippet: hit.snippet,
      folder: typeof hit.folder === "string" && hit.folder ? hit.folder : "all",
      tags: Array.isArray(hit.tags) ? hit.tags : [],
      starred: Boolean(hit.starred),
      remoteOnly: true,
    });
  }
  return extras;
}

export const EXPECTED_USER_HEADER = "X-ANILA-Expected-User-ID";

export function positiveUserId(value) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) return null;
  return value;
}

// 批次 mutation 必須帶「開始操作時」的畫面 user id。不是正整數就拒絕送出，
// 不能省略 header 讓 cookie 上的另一個帳號接手。
export function bindExpectedUser(authRequest, expectedId) {
  const id = positiveUserId(expectedId);
  if (typeof authRequest !== "function" || id == null) {
    return async () => {
      const error = new Error("請重新登入或重新整理後再操作");
      error.code = "expected-user-missing";
      throw error;
    };
  }
  const header = String(id);
  return (path, options = {}) => authRequest(path, {
    ...options,
    headers: {
      ...(options.headers || {}),
      [EXPECTED_USER_HEADER]: header,
    },
  });
}
