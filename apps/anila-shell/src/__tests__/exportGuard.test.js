// 匯出把關 / 頁首 / 落列 —— 補救計畫 W1-1 ②③④(前端側)。
//
// 主案例一律**營業秘密**:legacy `classified` boolean 對它是 False,所以吃
// boolean 的舊寫法會在這裡全綠。用絕對機密測則兩種寫法都綠。

import { describe, it, expect, vi } from "vitest";
import {
  EXPORT_RECEIPT_FAILURE_NOTICE,
  buildExportFile,
  prepareConversationExport,
} from "../runtime/exportGuard.js";

const receipt = (over = {}) => ({
  required: true,
  recorded: true,
  record_id: 7,
  allowed: true,
  classification_level: "無機密",
  exporter: "alice",
  exported_at: "2026-07-26T10:34:05+08:00",
  source_system: "ANILA 平台（CSP）",
  export_format: "markdown",
  header_lines: [],
  blocked_notice: null,
  ...over,
});

describe("② 匯出 gate", () => {
  it("營業秘密 —— 本地就擋,連落列都不打", async () => {
    const requestReceipt = vi.fn();
    const out = await prepareConversationExport({
      conversation: { id: 1, classificationLevel: "營業秘密", classified: false },
      format: "markdown",
      requestReceipt,
    });
    expect(out.ok).toBe(false);
    expect(out.notice).toContain("營業秘密");
    expect(out.notice).toContain("替代路徑");
    expect(requestReceipt).not.toHaveBeenCalled();
  });

  it("四個受控等級都擋", async () => {
    for (const level of ["營業秘密", "機密", "極機密", "絕對機密"]) {
      const out = await prepareConversationExport({
        conversation: { id: 1, classificationLevel: level },
        format: "json",
        requestReceipt: vi.fn(),
      });
      expect(out.ok).toBe(false);
    }
  });

  it("未知密等 fail-closed 擋下", async () => {
    const out = await prepareConversationExport({
      conversation: { id: 1, classificationLevel: "亂碼" },
      format: "json",
      requestReceipt: vi.fn(),
    });
    expect(out.ok).toBe(false);
  });

  it("無機密放行(不得誤擋)", async () => {
    const requestReceipt = vi.fn().mockResolvedValue(receipt());
    const out = await prepareConversationExport({
      conversation: { id: 1, classificationLevel: "無機密" },
      format: "markdown",
      requestReceipt,
    });
    expect(out.ok).toBe(true);
    expect(requestReceipt).toHaveBeenCalledWith(1, "markdown");
  });

  it("伺服器說不行就不行(client 的密等可能是舊的)", async () => {
    const requestReceipt = vi.fn().mockResolvedValue(
      receipt({ allowed: false, classification_level: "營業秘密", blocked_notice: "伺服器擋下:營業秘密" }),
    );
    const out = await prepareConversationExport({
      conversation: { id: 1, classificationLevel: "無機密" },
      format: "markdown",
      requestReceipt,
    });
    expect(out.ok).toBe(false);
    expect(out.notice).toContain("營業秘密");
  });

  it("allowed 欄位缺漏的舊/壞回應同樣不放行", async () => {
    const requestReceipt = vi.fn().mockResolvedValue({ classification_level: "無機密" });
    const out = await prepareConversationExport({
      conversation: { id: 1, classificationLevel: "無機密" },
      format: "markdown",
      requestReceipt,
    });
    expect(out.ok).toBe(false);
  });
});

describe("④ 落列失敗必須 fail-closed(斷線 = 不放行)", () => {
  it("網路失敗 → 不產檔 + 重試文案", async () => {
    const requestReceipt = vi.fn().mockRejectedValue(new Error("Failed to fetch"));
    const out = await prepareConversationExport({
      conversation: { id: 1, classificationLevel: "無機密" },
      format: "markdown",
      requestReceipt,
    });
    expect(out.ok).toBe(false);
    expect(out.notice).toBe(EXPORT_RECEIPT_FAILURE_NOTICE);
    expect(out.notice).toContain("重試");
  });

  it("回應不是物件 → 同樣不放行", async () => {
    const out = await prepareConversationExport({
      conversation: { id: 1, classificationLevel: "無機密" },
      format: "json",
      requestReceipt: vi.fn().mockResolvedValue(null),
    });
    expect(out.ok).toBe(false);
  });

  it("尚未建後端列的本地對話:不打落列,但頁首照蓋", async () => {
    const requestReceipt = vi.fn();
    const out = await prepareConversationExport({
      conversation: { id: "local-1", title: "新對話" },
      format: "markdown",
      exporter: "alice",
      requestReceipt,
    });
    expect(out.ok).toBe(true);
    expect(requestReceipt).not.toHaveBeenCalled();
    expect(out.header.markdown).toContain("密等：無機密");
    expect(out.header.markdown).toContain("匯出者：alice");
  });
});

describe("③ 頁首:markdown 與 json 兩種格式都要", () => {
  const header = {
    markdown: "> 密等：無機密\n> 匯出者：alice\n> 匯出時間：x\n> 來源系統：y",
    json: {
      classification_level: "無機密",
      exported_by: "alice",
      exported_at: "2026-07-26T10:34:05+08:00",
      source_system: "y",
      notice: ["密等：無機密", "匯出者：alice"],
    },
  };
  const messages = [
    { role: "user", text: "上個月營收多少？" },
    { role: "assistant", text: "三百二十萬。" },
  ];

  it("markdown:頁首在標題之前", () => {
    const file = buildExportFile({ title: "季度營收", messages, format: "markdown", header });
    expect(file.ext).toBe("md");
    expect(file.content.indexOf("密等：無機密")).toBeLessThan(
      file.content.indexOf("# 季度營收"),
    );
    expect(file.content).toContain("## 使用者");
    expect(file.content).toContain("三百二十萬。");
  });

  it("json:export_header 在最前面,且含四個欄位", () => {
    const file = buildExportFile({ title: "季度營收", messages, format: "json", header });
    expect(file.ext).toBe("json");
    const parsed = JSON.parse(file.content);
    expect(Object.keys(parsed)[0]).toBe("export_header");
    expect(parsed.export_header.classification_level).toBe("無機密");
    expect(parsed.export_header.notice.join("\n")).toContain("密等：無機密");
    expect(parsed.messages).toHaveLength(2);
  });

  it("頁首字樣在兩種格式裡都找得到(不能只做一半)", () => {
    for (const format of ["markdown", "json"]) {
      const file = buildExportFile({ title: "t", messages, format, header });
      expect(file.content).toContain("密等：無機密");
    }
  });

  it("收據上的密等會蓋進頁首(不是用 client 的舊值)", async () => {
    const out = await prepareConversationExport({
      conversation: { id: 1, classificationLevel: "無機密" },
      format: "markdown",
      exporter: "client-side-name",
      requestReceipt: vi.fn().mockResolvedValue(
        receipt({ classification_level: "無機密", exporter: "server-side-name" }),
      ),
    });
    expect(out.ok).toBe(true);
    expect(out.header.markdown).toContain("匯出者：server-side-name");
    expect(out.header.markdown).toContain("2026-07-26 10:34:05");
  });
});
