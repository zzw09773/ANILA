import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

import { Composer, MessageBubble } from "../chat.jsx";
import { AppliedSkillIndicator, SkillManager } from "../skills.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { buildPersistMeta } from "../runtime/messageMeta.js";
import { dispatchSseEvent } from "../runtime/sse.js";
import { filterSkills, slashQuery } from "../runtime/skills.js";
import { mountOrchestrator } from "./helpers/orchestrator.jsx";
import { createFakeBackend, namedEventFrame, scriptAnswer } from "./helpers/fakeBackend.js";

const SKILLS = [
  { id: 7, name: "週報", description: "整理本週重點", body: "不該送到瀏覽器請求" },
  { id: 8, name: "翻譯", description: "翻成英文", body: "另一份內容" },
];

function renderComposer(props = {}) {
  const onSend = props.onSend || vi.fn();
  render(
    <ConfirmProvider>
      <Composer onSend={onSend} agents={[]} skills={SKILLS} {...props} />
    </ConfirmProvider>,
  );
  return { onSend, ta: screen.getByRole("textbox") };
}

function typeAtEnd(ta, value) {
  Object.defineProperty(ta, "selectionStart", {
    configurable: true,
    get: () => ta.value.length,
  });
  fireEvent.change(ta, { target: { value } });
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false })));
});

describe("skill 選擇與套用標示", () => {
  it("輸入 / 會打開選擇清單", () => {
    const { ta } = renderComposer();
    expect(slashQuery("/週", 2)).toBe("週");
    expect(filterSkills(SKILLS, "週").map((item) => item.id)).toEqual([7]);
    typeAtEnd(ta, "/週");
    expect(screen.getByTestId("skill-picker")).toBeTruthy();
    expect(screen.getByRole("option", { name: /週報/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /翻譯/ })).toBeNull();
  });

  it("skill 按鈕打開清單，選了之後顯示可移除的套用 chip，送出只帶 id", () => {
    const { onSend, ta } = renderComposer();
    fireEvent.click(screen.getByRole("button", { name: "skill" }));
    fireEvent.click(screen.getByRole("option", { name: /週報/ }));
    expect(screen.getByTestId("skill-chip").textContent).toContain("套用：週報");
    expect(screen.queryByText("不該送到瀏覽器請求")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "移除 skill" }));
    expect(screen.queryByTestId("skill-chip")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "skill" }));
    fireEvent.click(screen.getByRole("option", { name: /週報/ }));
    typeAtEnd(ta, "請整理");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend).toHaveBeenCalledTimes(1);
    const meta = onSend.mock.calls[0][2];
    expect(meta.skillId).toBe(7);
    expect(JSON.stringify(meta)).not.toContain("不該送到瀏覽器請求");
    expect(screen.queryByTestId("skill-chip")).toBeNull();
  });

  it("送出失敗會把選中的 skill 放回", async () => {
    let settle;
    const onSend = vi.fn(() => new Promise((resolve) => {
      settle = resolve;
    }));
    const { ta } = renderComposer({ onSend });
    fireEvent.click(screen.getByRole("button", { name: "skill" }));
    fireEvent.click(screen.getByRole("option", { name: /週報/ }));
    typeAtEnd(ta, "請整理");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[0][2].skillId).toBe(7);
    expect(screen.getByRole("textbox").value).toBe("");
    expect(screen.queryByTestId("skill-chip")).toBeNull();

    await act(async () => { settle(false); });
    expect(screen.getByRole("textbox").value).toBe("請整理");
    expect(screen.getByTestId("skill-chip").textContent).toContain("套用：週報");
    expect(screen.queryByRole("button", { name: "還原未送出的訊息" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[1][2].skillId).toBe(7);
  });

  it("失敗時若已有新草稿，skill 留在還原列並能整份換回", async () => {
    let settle;
    const onSend = vi.fn(() => new Promise((resolve) => {
      settle = resolve;
    }));
    const { ta } = renderComposer({ onSend });
    fireEvent.click(screen.getByRole("button", { name: "skill" }));
    fireEvent.click(screen.getByRole("option", { name: /週報/ }));
    typeAtEnd(ta, "原始內容");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));

    fireEvent.click(screen.getByRole("button", { name: "skill" }));
    fireEvent.click(screen.getByRole("option", { name: /翻譯/ }));
    typeAtEnd(ta, "新草稿");
    await act(async () => { settle(false); });

    expect(screen.getByRole("textbox").value).toBe("新草稿");
    expect(screen.getByTestId("skill-chip").textContent).toContain("套用：翻譯");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("原始內容");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("套用：週報");

    fireEvent.click(screen.getByRole("button", { name: "還原未送出的訊息" }));
    expect(screen.getByRole("textbox").value).toBe("原始內容");
    expect(screen.getByTestId("skill-chip").textContent).toContain("套用：週報");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("新草稿");
    expect(screen.getByTestId("recoverable-draft").textContent).toContain("套用：翻譯");
  });

  it("預設提示詞直接送出仍帶著選中的 skill", () => {
    const onSend = vi.fn();
    renderComposer({
      onSend,
      presetPrompts: [{ id: "p1", label: "整理", config: { text: "請整理附件", autosend: true } }],
    });
    fireEvent.click(screen.getByRole("button", { name: "skill" }));
    fireEvent.click(screen.getByRole("option", { name: /週報/ }));
    fireEvent.click(screen.getByTitle("預設提示詞"));
    fireEvent.click(screen.getByRole("button", { name: /整理/ }));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0]).toBe("請整理附件");
    expect(onSend.mock.calls[0][2].skillId).toBe(7);
    expect(onSend.mock.calls[0][2].explicitAgents).toEqual([]);
    expect(screen.queryByTestId("skill-chip")).toBeNull();
  });

  it("回覆只在自動套用時顯示可展開的內容", () => {
    const { rerender } = render(
      <AppliedSkillIndicator skill={{ id: 7, name: "週報", body: "三點列出", mode: "auto" }} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "已自動套用：週報" }));
    expect(screen.getByTestId("applied-skill-body").textContent).toContain("三點列出");

    rerender(
      <MessageBubble
        msg={{ id: "a1", role: "assistant", text: "好", appliedSkill: { id: 7, name: "週報", body: "三點列出", mode: "auto" } }}
        agents={[]}
      />,
    );
    expect(screen.getByTestId("applied-skill").textContent).toContain("已自動套用：週報");

    rerender(
      <MessageBubble
        msg={{ id: "a2", role: "assistant", text: "好", appliedSkill: { id: 7, name: "週報", body: "三點列出", mode: "manual" } }}
        agents={[]}
      />,
    );
    expect(screen.queryByTestId("applied-skill")).toBeNull();
  });

  it("串流標頭只送 skill id，並把 anila.skill 交給回呼", async () => {
    const { streamChatCompletion } = await import("../runtime/sse.js");
    const encoder = new TextEncoder();
    const frame = 'event: anila.skill\ndata: {"id":7,"name":"週報","body":"三點列出","mode":"auto"}\n\n';
    let pulled = false;
    const fetchMock = vi.fn(async () => ({
      ok: true,
      headers: { get: () => null },
      body: {
        getReader: () => ({
          read: async () => {
            if (pulled) return { done: true, value: undefined };
            pulled = true;
            return { done: false, value: encoder.encode(frame) };
          },
        }),
      },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const onSkill = vi.fn();
    await streamChatCompletion({
      url: "/v1/chat/completions",
      payload: { model: "demo", messages: [{ role: "user", content: "hi" }] },
      skillId: 7,
      onSkill,
    });
    const headers = fetchMock.mock.calls[0][1].headers;
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(headers["X-ANILA-Skill-Id"]).toBe("7");
    expect(body.skill_body).toBeUndefined();
    expect(JSON.stringify(body)).not.toContain("三點列出");
    expect(onSkill).toHaveBeenCalledWith(expect.objectContaining({ id: 7, mode: "auto", name: "週報" }));

    const seen = vi.fn();
    dispatchSseEvent(
      { event: "anila.skill", data: '{"id":3,"name":"翻譯","mode":"manual"}' },
      { onSkill: seen },
    );
    expect(seen).toHaveBeenCalledWith(expect.objectContaining({ id: 3, name: "翻譯" }));

    const persisted = buildPersistMeta({}, {
      appliedSkill: { id: 7, name: "週報", body: "三點列出", mode: "auto" },
    });
    expect(persisted.applied_skill).toMatchObject({ id: 7, name: "週報", mode: "auto", body: "三點列出" });
  });

  it("比較回答要顯示手動套用", () => {
    render(
      <MessageBubble
        includeManualSkill
        msg={{
          id: "a3",
          role: "assistant",
          text: "好",
          appliedSkill: { id: 7, name: "週報", body: "三點列出", mode: "manual" },
        }}
        agents={[]}
      />,
    );
    expect(screen.getByTestId("applied-skill").textContent).toContain("套用：週報");
  });
});

function skillApi(targets, posts) {
  return vi.fn(async (url, init) => {
    const path = String(url);
    if (path.includes("/api/skills/publish-targets")) return targets;
    if (init?.method === "POST" && path === "/api/skills") {
      const body = JSON.parse(init.body);
      posts.push(body);
      return { id: 1, ...body, status: "published" };
    }
    return { skills: [] };
  });
}

async function fillSkillForm() {
  fireEvent.change(screen.getByLabelText("skill 名稱"), { target: { value: "週報" } });
  fireEvent.change(screen.getByLabelText("skill 用途"), { target: { value: "整理本週重點" } });
  fireEvent.change(screen.getByLabelText("skill 內容"), { target: { value: "請用三點列出" } });
}

describe("直接發布", () => {
  it("單位管理員可以選擇直接發布到自己管理的單位", async () => {
    const posts = [];
    const authRequest = skillApi(
      { campus: false, units: [{ id: 3, name: "東區" }, { id: 4, name: "東區一組" }] },
      posts,
    );
    render(
      <ConfirmProvider>
        <SkillManager authRequest={authRequest} user={{ id: 2, department_id: 3 }} />
      </ConfirmProvider>,
    );
    const select = await screen.findByLabelText("直接發布到…");
    expect(screen.getByRole("option", { name: "東區" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "東區一組" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "全院" })).toBeNull();
    fireEvent.change(select, { target: { value: "unit:3" } });
    await fillSkillForm();
    fireEvent.click(screen.getByRole("button", { name: "直接發布" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      scope: "unit",
      department_id: 3,
      name: "週報",
      submit: false,
    });
  });

  it("管理員可以選擇直接發布到全院", async () => {
    const posts = [];
    const authRequest = skillApi({ campus: true, units: [] }, posts);
    render(
      <ConfirmProvider>
        <SkillManager authRequest={authRequest} user={{ id: 1, role: "admin" }} />
      </ConfirmProvider>,
    );
    const select = await screen.findByLabelText("直接發布到…");
    expect(screen.getByRole("option", { name: "全院" })).toBeTruthy();
    fireEvent.change(select, { target: { value: "campus" } });
    await fillSkillForm();
    fireEvent.click(screen.getByRole("button", { name: "直接發布" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      scope: "campus",
      department_id: null,
      name: "週報",
      submit: false,
    });
  });

  it("一般使用者看不到直接發布", async () => {
    const authRequest = skillApi({ campus: false, units: [] }, []);
    render(
      <ConfirmProvider>
        <SkillManager authRequest={authRequest} user={{ id: 9, department_id: 3 }} />
      </ConfirmProvider>,
    );
    await screen.findByText("還沒有 skill。");
    expect(screen.queryByLabelText("直接發布到…")).toBeNull();
  });

  it("一般使用者可以把新 skill 送審到自己的單位或上層單位", async () => {
    const posts = [];
    const authRequest = skillApi(
      {
        campus: false,
        units: [],
        submit_units: [{ id: 3, name: "東區" }, { id: 1, name: "院本部" }],
      },
      posts,
    );
    render(
      <ConfirmProvider>
        <SkillManager authRequest={authRequest} user={{ id: 9, department_id: 3 }} />
      </ConfirmProvider>,
    );
    await screen.findByText("還沒有 skill。");
    expect(screen.queryByLabelText("直接發布到…")).toBeNull();
    const select = await screen.findByLabelText("送審到單位");
    expect(screen.getByRole("option", { name: "東區" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "院本部" })).toBeTruthy();
    fireEvent.change(select, { target: { value: "unit:3" } });
    await fillSkillForm();
    fireEvent.click(screen.getByRole("button", { name: "送審到單位" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      scope: "unit",
      department_id: 3,
      submit: true,
      name: "週報",
    });
  });

  it("清單上的送審到單位送給自己的單位", async () => {
    const posts = [];
    const authRequest = vi.fn(async (url, init) => {
      const path = String(url);
      if (path.includes("/api/skills/publish-targets")) {
        return { campus: false, units: [], submit_units: [{ id: 3, name: "東區" }] };
      }
      if (init?.method === "POST" && path.includes("/submit")) {
        posts.push(JSON.parse(init.body));
        return { id: 9, status: "pending" };
      }
      return {
        skills: [{
          id: 9,
          name: "週報",
          description: "整理本週重點",
          body: "三點",
          status: "draft",
          scope: "personal",
        }],
      };
    });
    render(
      <ConfirmProvider>
        <SkillManager authRequest={authRequest} user={{ id: 9, department_id: 3 }} />
      </ConfirmProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "送審到單位（東區）" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ scope: "unit", department_id: 3 });
  });
});

const TWO_AGENTS = [
  {
    id: "demo-agent",
    name: "示範助手",
    short: "demo",
    description: "測試用助手",
    endpoint_url: "https://example.invalid/v1",
    capabilities: {},
    requires_encryption: false,
  },
  {
    id: "second-agent",
    name: "第二助手",
    short: "second",
    description: "比較模式需要兩個",
    endpoint_url: "https://example.invalid/v1",
    capabilities: {},
    requires_encryption: false,
  },
];

describe("比較模式套用 skill", () => {
  it("把手動選的 skill 送到每一欄，並在各欄顯示套用", async () => {
    const backend = createFakeBackend({ agents: TWO_AGENTS });
    backend.route("GET", "/api/skills", (_req, { jsonResponse }) => jsonResponse({
      skills: [{ id: 7, name: "週報", description: "整理本週重點", body: "正文", status: "published" }],
    }));
    const skillEvent = namedEventFrame("anila.skill", {
      id: 7,
      name: "週報",
      body: "正文",
      mode: "manual",
    });
    backend.enqueueFrames([skillEvent, ...scriptAnswer("第一欄回答")]);
    backend.enqueueFrames([skillEvent, ...scriptAnswer("第二欄回答")]);
    await mountOrchestrator({ backend });

    const compareBtn = await waitFor(() => {
      const el = [...document.querySelectorAll("[title^='比較模式']")].find(
        (node) => !/需至少/.test(node.getAttribute("title") || ""),
      );
      expect(el).toBeTruthy();
      return el;
    });
    await act(async () => {
      fireEvent.click(compareBtn);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "skill" }));
    });
    const option = await screen.findByRole("option", { name: /週報/ });
    await act(async () => {
      fireEvent.click(option);
    });
    const box = screen.getByRole("textbox", { name: "傳訊息給 ANILA" });
    await act(async () => {
      fireEvent.change(box, { target: { value: "請整理" } });
    });
    await act(async () => {
      fireEvent.click(screen.getByLabelText("送出"));
    });

    await screen.findByText("第一欄回答");
    await screen.findByText("第二欄回答");
    const streams = backend.requests.filter(
      (req) => req.path === "/v1/chat/completions" && req.body?.stream !== false,
    );
    expect(streams).toHaveLength(2);
    for (const req of streams) {
      expect(req.init.headers["X-ANILA-Skill-Id"]).toBe("7");
    }
    const marks = screen.getAllByTestId("applied-skill");
    expect(marks).toHaveLength(2);
    for (const mark of marks) {
      expect(mark.textContent).toContain("套用：週報");
    }
  });
});
