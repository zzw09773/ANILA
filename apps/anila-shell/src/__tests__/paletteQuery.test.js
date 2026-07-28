// 命令面板查詢語法 —— Open WebUI 風格前綴過濾 + 封存可見性規則。
import { describe, it, expect } from "vitest";

import {
  filterConversationsForPalette,
  filterPaletteActions,
  parsePaletteQuery,
} from "../commands/paletteQuery.js";

const conv = (over) => ({
  id: 1,
  title: "對話",
  folder: "all",
  tags: [],
  starred: false,
  archived: false,
  ...over,
});

describe("parsePaletteQuery", () => {
  it("returns empty filters for plain text", () => {
    const parsed = parsePaletteQuery("特休 怎麼算");
    expect(parsed.text).toBe("特休 怎麼算");
    expect(parsed.hasFilters).toBe(false);
    expect(parsed.filters).toEqual({ folder: null, tag: null, starred: false, archived: false });
  });

  it("parses folder: / tag: with values", () => {
    const parsed = parsePaletteQuery("folder:hr tag:合約 條款");
    expect(parsed.filters.folder).toBe("hr");
    expect(parsed.filters.tag).toBe("合約");
    expect(parsed.text).toBe("條款");
    expect(parsed.hasFilters).toBe(true);
  });

  it("treats bare starred: / archived: / pinned: as flags", () => {
    expect(parsePaletteQuery("starred:").filters.starred).toBe(true);
    expect(parsePaletteQuery("pinned:").filters.starred).toBe(true);
    expect(parsePaletteQuery("archived:").filters.archived).toBe(true);
    expect(parsePaletteQuery("archived:true").filters.archived).toBe(true);
    expect(parsePaletteQuery("archived:false").filters.archived).toBe(false);
  });

  it("is case-insensitive on the prefix", () => {
    expect(parsePaletteQuery("TAG:hr").filters.tag).toBe("hr");
  });

  it("is null-safe", () => {
    expect(parsePaletteQuery(undefined).text).toBe("");
    expect(parsePaletteQuery("").hasFilters).toBe(false);
  });
});

describe("filterConversationsForPalette", () => {
  const rows = [
    conv({ id: 1, title: "特休怎麼算", folder: "usr-hr", tags: ["hr"] }),
    conv({ id: 2, title: "封存的舊案", archived: true, tags: ["hr"] }),
    conv({ id: 3, title: "加星的重點", starred: true }),
    conv({ id: 4, title: "合約審查", folder: "usr-legal", tags: ["合約", "legal"] }),
  ];

  it("hides archived conversations from the default (no query) list", () => {
    const out = filterConversationsForPalette(rows, parsePaletteQuery(""));
    expect(out.map((c) => c.id)).toEqual([1, 3, 4]);
  });

  it("still finds archived conversations via free-text search", () => {
    const out = filterConversationsForPalette(rows, parsePaletteQuery("封存"));
    expect(out.map((c) => c.id)).toEqual([2]);
  });

  it("archived: shows only archived ones", () => {
    const out = filterConversationsForPalette(rows, parsePaletteQuery("archived:"));
    expect(out.map((c) => c.id)).toEqual([2]);
  });

  it("starred: shows only starred ones", () => {
    expect(
      filterConversationsForPalette(rows, parsePaletteQuery("starred:")).map((c) => c.id),
    ).toEqual([3]);
  });

  it("tag: filters on tags (substring allowed)", () => {
    expect(
      filterConversationsForPalette(rows, parsePaletteQuery("tag:合約")).map((c) => c.id),
    ).toEqual([4]);
    // 封存的那筆也有 hr,但沒有 archived: 就不該出現
    expect(
      filterConversationsForPalette(rows, parsePaletteQuery("tag:hr")).map((c) => c.id),
    ).toEqual([1]);
  });

  it("folder: resolves by id and by display name", () => {
    const folders = [{ id: "usr-legal", name: "法務" }];
    expect(
      filterConversationsForPalette(rows, parsePaletteQuery("folder:usr-legal"), folders).map((c) => c.id),
    ).toEqual([4]);
    expect(
      filterConversationsForPalette(rows, parsePaletteQuery("folder:法務"), folders).map((c) => c.id),
    ).toEqual([4]);
  });

  it("combines a filter with free text", () => {
    expect(
      filterConversationsForPalette(rows, parsePaletteQuery("tag:legal 合約"), []).map((c) => c.id),
    ).toEqual([4]);
    expect(
      filterConversationsForPalette(rows, parsePaletteQuery("tag:legal 無關"), []).map((c) => c.id),
    ).toEqual([]);
  });

  it("matches the server snippet as well as the title", () => {
    const hits = [conv({ id: 9, title: "無關標題", snippet: "裡面提到特休" })];
    expect(filterConversationsForPalette(hits, parsePaletteQuery("特休")).map((c) => c.id)).toEqual([9]);
  });

  it("is null-safe", () => {
    expect(filterConversationsForPalette(null, parsePaletteQuery(""))).toEqual([]);
  });
});

describe("filterPaletteActions", () => {
  const actions = [
    { id: "new-chat", label: "新對話", keywords: ["new chat", "開新"] },
    { id: "open-memory", label: "開啟設定 → 記憶", keywords: ["memory"] },
  ];

  it("returns everything for an empty query", () => {
    expect(filterPaletteActions(actions, "")).toHaveLength(2);
  });

  it("matches label and keywords", () => {
    expect(filterPaletteActions(actions, "記憶").map((a) => a.id)).toEqual(["open-memory"]);
    expect(filterPaletteActions(actions, "MEMORY").map((a) => a.id)).toEqual(["open-memory"]);
    expect(filterPaletteActions(actions, "新").map((a) => a.id)).toEqual(["new-chat"]);
  });

  it("returns nothing when no action matches", () => {
    expect(filterPaletteActions(actions, "zzz")).toEqual([]);
  });
});
