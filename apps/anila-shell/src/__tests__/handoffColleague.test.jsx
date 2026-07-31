// 「交給同事」前端不變式。
//
// 2026-07-30 這顆按鈕被拿掉的根因是「送出的酬載沒有 to_user_id」——輸入框
// 只是自由文字。這裡釘住的就是那個缺陷不會回來:
//   1. 沒有從清單挑到人，送出鍵是 disabled，而且按下去也不會發請求。
//   2. 真的送出時，body 必須帶 to_user_id，而且是清單裡那筆的 id。
//   3. 收件匣只顯示「別人交給我的、還沒處理的」。

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { HandoffToColleague, HandoffInbox } from "../collab.jsx";
import { incomingPendingHandoffs } from "../runtime/conversations.js";

const DIRECTORY = [
  { id: 7, username: "bob.lin", department: "資訊所" },
  { id: 9, username: "bobby.wu", department: null },
];

function directoryRequest(overrides = {}) {
  return vi.fn(async (path, options) => {
    if (path.startsWith("/api/directory/users")) return DIRECTORY;
    if (path === "/api/handoffs" && options?.method === "POST") {
      return { id: 1, conversation_id: 42, to_user_id: 7, status: "pending" };
    }
    if (overrides[path]) return overrides[path];
    throw new Error(`unexpected call ${path}`);
  });
}

describe("HandoffToColleague — 沒挑到人就送不出去", () => {
  it("送出鍵一開始是 disabled，而且點下去不會發任何請求", async () => {
    const authRequest = directoryRequest();
    render(<HandoffToColleague conversationId={42} authRequest={authRequest} />);

    const submit = screen.getByRole("button", { name: "送出交接請求" });
    expect(submit).toBeDisabled();

    fireEvent.click(submit);
    await waitFor(() => {
      expect(
        authRequest.mock.calls.filter(([, o]) => o?.method === "POST"),
      ).toHaveLength(0);
    });
  });

  it("打字只會查通訊錄，不會建立交接", async () => {
    const authRequest = directoryRequest();
    render(<HandoffToColleague conversationId={42} authRequest={authRequest} />);

    fireEvent.change(
      screen.getByPlaceholderText("輸入同事帳號關鍵字（例如 bob）"),
      { target: { value: "bob" } },
    );

    await screen.findByRole("button", { name: "bob.lin · 資訊所" });
    expect(
      authRequest.mock.calls.filter(([, o]) => o?.method === "POST"),
    ).toHaveLength(0);
    // 沒有單位的同仁不要留一個孤兒分隔點。
    expect(screen.getByRole("button", { name: "bobby.wu" })).toBeInTheDocument();
  });

  it("挑到人之後送出，酬載真的帶 to_user_id", async () => {
    const authRequest = directoryRequest();
    render(<HandoffToColleague conversationId={42} authRequest={authRequest} />);

    fireEvent.change(
      screen.getByPlaceholderText("輸入同事帳號關鍵字（例如 bob）"),
      { target: { value: "bob" } },
    );
    fireEvent.click(await screen.findByRole("button", { name: "bob.lin · 資訊所" }));

    const submit = screen.getByRole("button", { name: "送出交接請求" });
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => {
      const post = authRequest.mock.calls.find(([, o]) => o?.method === "POST");
      expect(post).toBeTruthy();
      expect(post[0]).toBe("/api/handoffs");
      const body = JSON.parse(post[1].body);
      expect(body.to_user_id).toBe(7);
      expect(body.conversation_id).toBe(42);
    });
    await screen.findByText(/已送出交接請求給「bob.lin · 資訊所」/);
  });
});

describe("incomingPendingHandoffs", () => {
  const rows = [
    { id: 1, status: "pending", to_user_id: 5 },
    { id: 2, status: "pending", to_user_id: 6 },
    { id: 3, status: "accepted", to_user_id: 5 },
    null,
  ];

  it("只留下交給我、還沒處理的", () => {
    expect(incomingPendingHandoffs(rows, 5).map((r) => r.id)).toEqual([1]);
  });

  it("沒有使用者 id 或不是陣列就回空陣列", () => {
    expect(incomingPendingHandoffs(rows, undefined)).toEqual([]);
    expect(incomingPendingHandoffs(null, 5)).toEqual([]);
  });
});

describe("HandoffInbox — 收件人這一端真的按得到", () => {
  const inbox = [
    {
      id: 11,
      conversation_id: 42,
      to_user_id: 5,
      status: "pending",
      from_username: "alice.chen",
      conversation_title: "採購案討論",
      note: "後續請接洽採購",
    },
    { id: 12, conversation_id: 43, to_user_id: 99, status: "pending" },
  ];

  function inboxRequest() {
    return vi.fn(async (path, options) => {
      if (path === "/api/handoffs" && (!options || options.method === "GET")) {
        return inbox;
      }
      if (path === "/api/handoffs/11/accept") return { id: 11, status: "accepted" };
      if (path === "/api/handoffs/11/reject") return { id: 11, status: "rejected" };
      throw new Error(`unexpected call ${path}`);
    });
  }

  it("只顯示交給我的那一筆，並帶出送出者與標題", async () => {
    const authRequest = inboxRequest();
    render(<HandoffInbox authRequest={authRequest} currentUserId={5} />);
    await screen.findByText(/alice.chen/);
    expect(screen.getByText(/採購案討論/)).toBeInTheDocument();
    expect(screen.queryByText(/#43/)).not.toBeInTheDocument();
  });

  it("按接受會打 accept 端點，並提示重新整理", async () => {
    const authRequest = inboxRequest();
    render(<HandoffInbox authRequest={authRequest} currentUserId={5} />);
    fireEvent.click(await screen.findByRole("button", { name: "接受" }));

    await waitFor(() => {
      expect(authRequest).toHaveBeenCalledWith("/api/handoffs/11/accept", {
        method: "POST",
      });
    });
    await screen.findByText(/已接手「採購案討論」/);
  });

  it("沒有待處理的交接就完全不占版面", async () => {
    const authRequest = vi.fn(async () => []);
    const { container } = render(
      <HandoffInbox authRequest={authRequest} currentUserId={5} />,
    );
    await waitFor(() => expect(authRequest).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
