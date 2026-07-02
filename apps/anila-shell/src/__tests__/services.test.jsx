// 專案入口（Service Platform）— fetch fallback、卡片渲染、launch 分流、iframe 覆蓋層、失敗路徑。

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import {
  ServicesPanel,
  fetchServices,
  resolveLaunch,
  normalizeServiceCard,
} from "../services.jsx";

function notFound() {
  return Object.assign(new Error("not found"), { status: 404 });
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
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
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
      .mockRejectedValueOnce(new Error("政策拒絕啟動"));
    const toast = vi.fn();
    render(<ServicesPanel open onClose={() => {}} request={request} toast={toast} />);
    fireEvent.click(await screen.findByText("壞掉服務"));
    expect(await screen.findByText("政策拒絕啟動")).toBeTruthy();
    await waitFor(() => expect(toast).toHaveBeenCalledWith("政策拒絕啟動", { tone: "error" }));
  });
});
