// 專案入口（Service Platform）— fetch fallback、卡片渲染、launch 分流、iframe 覆蓋層、失敗路徑。

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import {
  ServicesPanel,
  fetchServices,
  isSameOriginUrl,
  resolveLaunch,
  normalizeServiceCard,
} from "../services.jsx";

function notFound() {
  return Object.assign(new Error("not found"), { status: 404 });
}

function httpError(status, detail) {
  return Object.assign(new Error(detail), { status });
}

// window.open 回傳「開起來的視窗」的樣子;回 null 才代表被擋掉。
function openedWindow() {
  return {};
}

// ---------------------------------------------------------------------
// fetchServices — fallback chain
// ---------------------------------------------------------------------

describe("fetchServices", () => {
  it("uses /api/services when available (registry mode)", async () => {
    const request = vi.fn().mockResolvedValueOnce({
      services: [{ id: 2, name: "MLSteam", url: "u", launch_mode: "iframe" }],
    });
    const services = await fetchServices(request);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("/api/services");
    expect(services).toHaveLength(1);
    expect(services[0].launchMode).toBe("iframe");
    expect(services[0].__legacy).toBe(false);
  });

  it("falls back to /api/platform-links on 404", async () => {
    const request = vi
      .fn()
      .mockRejectedValueOnce(notFound())
      .mockResolvedValueOnce([{ id: 1, name: "GitLab", url: "https://gl" }]);
    const services = await fetchServices(request);
    expect(request).toHaveBeenNthCalledWith(1, "/api/services");
    expect(request).toHaveBeenNthCalledWith(2, "/api/platform-links");
    expect(services).toHaveLength(1);
    expect(services[0].__legacy).toBe(true);
    expect(services[0].name).toBe("GitLab");
    // legacy 服務預設 new_tab。
    expect(services[0].launchMode).toBe("new_tab");
  });

  it("rethrows non-404 errors instead of falling back", async () => {
    const boom = Object.assign(new Error("boom"), { status: 500 });
    const request = vi.fn().mockRejectedValueOnce(boom);
    await expect(fetchServices(request)).rejects.toThrow("boom");
    expect(request).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------
// resolveLaunch — registry vs legacy
// ---------------------------------------------------------------------

describe("resolveLaunch", () => {
  it("POSTs to the launch endpoint for registry services", async () => {
    const request = vi.fn().mockResolvedValueOnce({
      mode: "new_tab",
      launch_url: "https://svc/launch",
      launch_id: "L1",
    });
    const svc = normalizeServiceCard({ id: 7, name: "n8n", url: "https://n8n" });
    const res = await resolveLaunch(request, svc);
    expect(request).toHaveBeenCalledWith("/api/services/7/launch", expect.objectContaining({ method: "POST" }));
    expect(res.launch_url).toBe("https://svc/launch");
  });

  it("opens legacy links directly without a launch call", async () => {
    const request = vi.fn();
    const svc = normalizeServiceCard({ id: 1, name: "GitLab", url: "https://gl" }, { legacy: true });
    const res = await resolveLaunch(request, svc);
    expect(request).not.toHaveBeenCalled();
    expect(res).toEqual({ mode: "new_tab", launch_url: "https://gl", launch_id: null });
  });
});

// ---------------------------------------------------------------------
// ServicesPanel — render + launch flows
// ---------------------------------------------------------------------

describe("ServicesPanel", () => {
  it("renders nothing when closed", () => {
    const { container } = render(<ServicesPanel open={false} onClose={() => {}} request={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders service cards with name and description", async () => {
    const request = vi.fn().mockResolvedValue([
      { id: 1, name: "GitLab", description: "程式碼託管", url: "https://gl", launch_mode: "new_tab" },
    ]);
    render(<ServicesPanel open onClose={() => {}} request={request} />);
    expect(await screen.findByText("GitLab")).toBeTruthy();
    expect(screen.getByText("程式碼託管")).toBeTruthy();
  });

  it("launches new_tab services via window.open with noopener", async () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(openedWindow);
    const request = vi
      .fn()
      .mockResolvedValueOnce([{ id: 7, name: "n8n", url: "https://n8n", launch_mode: "new_tab" }])
      .mockResolvedValueOnce({ mode: "new_tab", launch_url: "https://n8n/launch", launch_id: "L1" });
    render(<ServicesPanel open onClose={() => {}} request={request} />);
    fireEvent.click(await screen.findByText("n8n"));
    await waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith("https://n8n/launch", "_blank", "noopener"),
    );
    openSpy.mockRestore();
  });

  it("opens then closes the sandboxed iframe overlay for iframe services", async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce([{ id: 3, name: "MLSteam", url: "https://ml", launch_mode: "iframe" }])
      .mockResolvedValueOnce({ mode: "iframe", launch_url: "https://ml/embed", launch_id: "L2" });
    render(<ServicesPanel open onClose={() => {}} request={request} />);
    fireEvent.click(await screen.findByText("MLSteam"));

    const frame = await screen.findByTitle("MLSteam");
    expect(frame.getAttribute("src")).toBe("https://ml/embed");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin allow-forms");
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");

    // 安全提示橫幅含服務名。
    expect(screen.getByRole("note").textContent).toContain("MLSteam");

    fireEvent.click(screen.getByLabelText("關閉服務"));
    await waitFor(() => expect(screen.queryByTitle("MLSteam")).toBeNull());
  });

  it("surfaces an inline error and toast when launch fails", async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce([{ id: 9, name: "壞掉服務", url: "https://x", launch_mode: "new_tab" }])
      .mockRejectedValueOnce(httpError(403, "政策拒絕啟動"));
    const toast = vi.fn();
    render(<ServicesPanel open onClose={() => {}} request={request} toast={toast} />);
    fireEvent.click(await screen.findByText("壞掉服務"));
    const alert = await screen.findByRole("alert");
    // 使用者讀的第一行:講得出是哪個服務、以及能做什麼。
    expect(alert.textContent).toContain("壞掉服務");
    expect(alert.textContent).toContain("聯絡平台管理員");
    // 後端原文沒有被丟掉,只是退到第二行。
    expect(alert.textContent).toContain("政策拒絕啟動");
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.stringContaining("壞掉服務"), {
        tone: "error",
      }),
    );
  });
});

// ---------------------------------------------------------------------
// 啟動失敗必須看得見(2026-08-02:五張卡按了「什麼也沒發生」)
// ---------------------------------------------------------------------

describe("ServicesPanel — 啟動失敗一定看得見", () => {
  async function clickAndGetAlert(launchOutcome, { toast = vi.fn() } = {}) {
    const request = vi
      .fn()
      .mockResolvedValueOnce([
        { id: 2, name: "ANILA", url: "/anila", launch_mode: "new_tab" },
      ]);
    if (launchOutcome instanceof Error) request.mockRejectedValueOnce(launchOutcome);
    else request.mockResolvedValueOnce(launchOutcome);
    render(<ServicesPanel open onClose={() => {}} request={request} toast={toast} />);
    fireEvent.click(await screen.findByText("ANILA"));
    return { alert: await screen.findByRole("alert"), toast };
  }

  it("400 不再把 entry_url 這種欄位名當成對使用者說的第一句話", async () => {
    const { alert } = await clickAndGetAlert(
      httpError(400, "服務 entry_url 必須是 http(s) URL"),
    );
    const headline = alert.firstChild.textContent;
    expect(headline).toContain("「ANILA」");
    expect(headline).toContain("註冊設定有誤");
    expect(headline).not.toContain("entry_url");
    // 原文仍在,給管理員轉述用。
    expect(alert.textContent).toContain("服務 entry_url 必須是 http(s) URL");
  });

  it("停用(409)說的是「已停用,找管理員」", async () => {
    const { alert } = await clickAndGetAlert(httpError(409, "服務已停用"));
    expect(alert.textContent).toContain("已停用");
    expect(alert.textContent).toContain("平台管理員");
  });

  it("尚未開放(503)說的是「還沒開」,不是「壞了」也不是「不存在」", async () => {
    const { alert } = await clickAndGetAlert(
      httpError(503, "ANILA LM 尚未開放,此功能仍在整備中"),
    );
    // 第一行是**這張卡**的說法(帶服務名),不是後端原文照搬。
    expect(alert.firstChild.textContent).toContain("「ANILA」");
    expect(alert.firstChild.textContent).toContain("尚未開放");
    expect(alert.firstChild.textContent).not.toContain("找不到");
  });

  it("成功開新分頁時也給一行回饋 —— 點下去永遠不會「什麼都沒發生」", async () => {
    // ⚠ window.open(url,'_blank','noopener') 依規格**恆回傳 null**,所以不能
    //   用回傳值判斷有沒有被擋。這條釘住「不靠回傳值猜」這個決定:
    //   即使 mock 回 null(＝真實瀏覽器帶 noopener 時的行為),也不准報錯。
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    const request = vi
      .fn()
      .mockResolvedValueOnce([
        { id: 2, name: "ANILA", url: "/anila", launch_mode: "new_tab" },
      ])
      .mockResolvedValueOnce({
        mode: "new_tab",
        launch_url: "/anila?launch_token=t",
        launch_id: "L1",
      });
    const toast = vi.fn();
    render(<ServicesPanel open onClose={() => {}} request={request} toast={toast} />);
    fireEvent.click(await screen.findByText("ANILA"));
    const status = await screen.findByRole("status");
    expect(status.textContent).toContain("「ANILA」");
    expect(status.textContent).toContain("彈出視窗");
    // 成功不得被說成失敗。
    expect(screen.queryByRole("alert")).toBeNull();
    expect(toast).not.toHaveBeenCalledWith(expect.anything(), { tone: "error" });
    expect(openSpy).toHaveBeenCalledWith("/anila?launch_token=t", "_blank", "noopener");
    openSpy.mockRestore();
  });

  it("noopener 沒有被拿掉（不為了偵測而拆掉瀏覽器層級的保護）", async () => {
    // 可觀察的事實:交給 window.open 的 features 一定帶 noopener,所以開出去的
    // 分頁拿不到 opener 參照(拿得到就能回頭改我們這一頁的 location)。
    // ⚠ 這條斷言的是**傳出去的引數**,不是 services.jsx 的原始碼字串。原始碼
    //   比對一次重排版就壞,卻擋不住真正的退化;而寫死整串 features 又會把
    //   「再加上 noreferrer」這種正確的加強誤判成退化。所以切成 token 比對。
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    const request = vi
      .fn()
      .mockResolvedValueOnce([
        { id: 2, name: "ANILA", url: "/anila", launch_mode: "new_tab" },
      ])
      .mockResolvedValueOnce({
        mode: "new_tab",
        launch_url: "/anila?launch_token=t",
        launch_id: "L1",
      });
    render(<ServicesPanel open onClose={() => {}} request={request} toast={vi.fn()} />);
    fireEvent.click(await screen.findByText("ANILA"));
    await waitFor(() => expect(openSpy).toHaveBeenCalled());

    const [, target, features] = openSpy.mock.calls[0];
    expect(target).toBe("_blank");
    // 第三個引數被省略 / 改成 "" / 換成別的旗標,都會在這裡變紅。
    expect(String(features ?? "").split(/[\s,]+/)).toContain("noopener");
    openSpy.mockRestore();
  });

  it("完全拿不到網址時報錯,不是靜靜地什麼也沒開", async () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(openedWindow);
    const request = vi
      .fn()
      // 卡片本身也沒有 url,launch 又沒回 launch_url → 真的無處可去。
      .mockResolvedValueOnce([{ id: 5, name: "沒網址服務", launch_mode: "new_tab" }])
      .mockResolvedValueOnce({ mode: "new_tab", launch_url: "", launch_id: "L0" });
    render(<ServicesPanel open onClose={() => {}} request={request} toast={vi.fn()} />);
    fireEvent.click(await screen.findByText("沒網址服務"));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("沒網址服務");
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------
// 同源相對 launch_url + iframe 沙箱
// ---------------------------------------------------------------------

describe("isSameOriginUrl", () => {
  it("相對路徑算同源", () => {
    expect(isSameOriginUrl("/anila?launch_token=t")).toBe(true);
  });

  it("同 origin 的絕對 URL 算同源", () => {
    expect(isSameOriginUrl(`${window.location.origin}/n8n`)).toBe(true);
  });

  it("別的主機不算同源", () => {
    expect(isSameOriginUrl("https://aiops.example.org:4443/")).toBe(false);
  });

  it("解析不出來時保守地當同源(寧可少開一個 iframe)", () => {
    expect(isSameOriginUrl("")).toBe(true);
    expect(isSameOriginUrl("http://[::1")).toBe(true);
  });
});

describe("ServicesPanel — 同源服務不進假沙箱的 iframe", () => {
  it("同源 + iframe 模式改開新分頁,並且說明為什麼", async () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(openedWindow);
    const toast = vi.fn();
    const request = vi
      .fn()
      .mockResolvedValueOnce([
        { id: 2, name: "ANILA", url: "/anila", launch_mode: "iframe" },
      ])
      .mockResolvedValueOnce({
        mode: "iframe",
        launch_url: "/anila?launch_token=t",
        launch_id: "L9",
      });
    render(<ServicesPanel open onClose={() => {}} request={request} toast={toast} />);
    fireEvent.click(await screen.findByText("ANILA"));
    await waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(
        "/anila?launch_token=t",
        "_blank",
        "noopener",
      ),
    );
    // 沒有掛出那條「內容於受限沙箱中執行」的假保證。
    expect(screen.queryByTitle("ANILA")).toBeNull();
    expect(screen.queryByRole("note")).toBeNull();
    expect(toast).toHaveBeenCalledWith(
      expect.stringContaining("平台自家服務"),
      { tone: "info" },
    );
    openSpy.mockRestore();
  });

  it("跨主機服務照舊走沙箱 iframe,沙箱屬性一格都沒放寬", async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce([
        {
          id: 7,
          name: "MLSteam",
          url: "https://aiops.example.org:4443/",
          launch_mode: "iframe",
        },
      ])
      .mockResolvedValueOnce({
        mode: "iframe",
        launch_url: "https://aiops.example.org:4443/?launch_token=t",
        launch_id: "L10",
      });
    render(<ServicesPanel open onClose={() => {}} request={request} toast={vi.fn()} />);
    fireEvent.click(await screen.findByText("MLSteam"));
    const frame = await screen.findByTitle("MLSteam");
    expect(frame.getAttribute("sandbox")).toBe(
      "allow-scripts allow-same-origin allow-forms",
    );
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
  });
});
