import { describe, it, expect } from "vitest";
import {
  SCORE_LABEL,
  citationIdentity,
  groupCitations,
  isUrlCitation,
  maxClassificationLevel,
  normalizeCitation,
  scoreToPercent,
} from "../citationUtils.js";

describe("scoreToPercent (cosine similarity, higher = closer)", () => {
  it("maps similarity 0.87 → 87%", () => {
    expect(scoreToPercent(0.87)).toBe(87);
  });

  it("maps 1.0 → 100 and 0 → 0 (not inverted)", () => {
    expect(scoreToPercent(1)).toBe(100);
    expect(scoreToPercent(0)).toBe(0);
  });

  it("clamps out-of-range values", () => {
    expect(scoreToPercent(1.2)).toBe(100);
    expect(scoreToPercent(-0.1)).toBe(0);
  });

  it("returns null for missing / non-finite", () => {
    expect(scoreToPercent(null)).toBeNull();
    expect(scoreToPercent(undefined)).toBeNull();
    expect(scoreToPercent(NaN)).toBeNull();
  });
});

describe("normalizeCitation (backend + legacy shapes)", () => {
  it("maps retrieval wire fields", () => {
    const n = normalizeCitation(
      {
        index: 2,
        chunk_id: 77,
        document_id: 9,
        filename: "policy.txt",
        chunk_key: "p:3",
        excerpt: "規範內容…",
        score: 0.91,
        classification_level: "機密",
      },
      0,
    );
    expect(n.n).toBe(2);
    expect(n.title).toBe("policy.txt");
    expect(n.section).toBe("p:3");
    expect(n.snippet).toBe("規範內容…");
    expect(n.score).toBe(0.91);
    expect(n.classificationLevel).toBe("機密");
    expect(n.documentId).toBe(9);
    expect(n.id).toBe("77");
  });

  it("falls back to legacy title/snippet/section and array index", () => {
    const n = normalizeCitation(
      { title: "舊標題", section: "§2", snippet: "摘錄", score: 0.5 },
      3,
    );
    expect(n.n).toBe(4);
    expect(n.title).toBe("舊標題");
    expect(n.section).toBe("§2");
    expect(n.snippet).toBe("摘錄");
  });
});

describe("groupCitations", () => {
  const hits = [
    {
      index: 1, chunk_id: 1, document_id: 10, filename: "a.pdf",
      chunk_key: "a:1", excerpt: "one", score: 0.9, classification_level: "營業秘密",
    },
    {
      index: 2, chunk_id: 2, document_id: 10, filename: "a.pdf",
      chunk_key: "a:2", excerpt: "two", score: 0.8, classification_level: "機密",
    },
    {
      index: 3, chunk_id: 3, document_id: 20, filename: "b.pdf",
      chunk_key: "b:1", excerpt: "three", score: 0.7, classification_level: "無機密",
    },
    {
      index: 4, id: "web-1", title: "外部頁", source_uri: "https://example.com/x",
      snippet: "url body", score: 0.6,
    },
  ];

  it("groups same document_id chunks and separates URL sources", () => {
    const { documents, total } = groupCitations(hits);
    expect(total).toBe(4);
    expect(documents).toHaveLength(3);
    expect(documents[0].title).toBe("a.pdf");
    expect(documents[0].items).toHaveLength(2);
    expect(documents[0].items.map((i) => i.n)).toEqual([1, 2]);
    expect(documents[0].classificationLevel).toBe("機密");
    expect(documents[1].title).toBe("b.pdf");
    expect(documents[1].items).toHaveLength(1);
    expect(documents[2].kind).toBe("url");
    expect(documents[2].items).toHaveLength(1);
    expect(documents[2].items[0].sourceUri).toBe("https://example.com/x");
  });

  it("preserves [N] numbering across groups", () => {
    const { documents } = groupCitations(hits);
    const allN = documents.flatMap((g) => g.items.map((i) => i.n));
    expect(allN).toEqual([1, 2, 3, 4]);
  });
});

describe("isUrlCitation / maxClassificationLevel / identity", () => {
  it("does not treat document-backed hits with source_uri as URL bucket", () => {
    expect(isUrlCitation({
      filename: "a.pdf", document_id: 1, source_uri: "https://intranet/doc/1",
    })).toBe(false);
    expect(isUrlCitation({ source_uri: "https://example.com" })).toBe(true);
  });

  it("orders five-level classification by severity", () => {
    expect(maxClassificationLevel(["營業秘密", "絕對機密", "機密"])).toBe("絕對機密");
    expect(maxClassificationLevel([])).toBeNull();
  });

  it("citationIdentity prefers id then chunk_id", () => {
    expect(citationIdentity({ id: "x", chunk_id: 9 })).toBe("x");
    expect(citationIdentity({ chunk_id: 9 })).toBe("9");
  });

  it("exposes the honest score label", () => {
    expect(SCORE_LABEL).toBe("餘弦相似度");
  });
});
