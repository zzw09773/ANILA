import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, within, cleanup } from "@testing-library/react";

import { Composer, MessageBubble } from "../chat.jsx";
import { SkillManager } from "../skills.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { buildPersistMeta } from "../runtime/messageMeta.js";
import { mapServerMessage } from "../runtime/serverMappers.js";
import {
  appliedSkillFromMeta,
  appliedSkillList,
  cleanSkillIds,
  groupSkills,
  maxStackFrom,
  skillHeaders,
} from "../runtime/skills.js";
import { mountOrchestrator, waitFor } from "./helpers/orchestrator.jsx";
import { createFakeBackend, namedEventFrame, scriptAnswer } from "./helpers/fakeBackend.js";

const SKILLS = [
  { id: 7, name: "週報", description: "整理本週重點", scope: "personal", favorite: true },
  { id: 8, name: "翻譯", description: "翻成英文", scope: "personal", last_used_at: "2026-10-11T02:00:00Z" },
  { id: 9, name: "公文", description: "改成公文格式", scope: "unit", department_name: "資訊室" },
  { id: 10, name: "簡報", description: "整理成投影片大綱", scope: "campus" },
  { id: 11, name: "摘要", description: "三點摘要", scope: "personal" },
];

function renderComposer(props = {}) {
  const onSend = props.onSend || vi.fn(() => true);
  render(
    <ConfirmProvider>
      <Composer onSend={onSend} agents={[]} skills={SKILLS} {...props} />
    </ConfirmProvider>,
  );
  return { onSend, ta: screen.getByRole("textbox", { name: "傳訊息給 ANILA" }) };
}

function typeAtEnd(ta, value) {
  Object.defineProperty(ta, "selectionStart", { configurable: true, get: () => ta.value.length });
  Object.defineProperty(ta, "selectionEnd", { configurable: true, get: () => ta.value.length });
  fireEvent.change(ta, { target: { value } });
}

function openMenu() {
  fireEvent.click(screen.getByRole("button", { name: "skill" }));
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false })));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("skills runtime：疊加", () => {
  it("skillHeaders 照順序、去重，並保留舊標頭的第一個", () => {
    expect(skillHeaders([12, 34, 12, -1, "x"])).toEqual({ "X-ANILA-Skill-Ids": "12,34", "X-ANILA-Skill-Id": "12" });
    expect(skillHeaders([])).toBeNull();
    expect(cleanSkillIds([3, "4", 0, 3])).toEqual([3, 4]);
  });

  it("maxStackFrom 讀 API 給的上限，沒有就 3", () => {
    expect(maxStackFrom({ max_stack: 5 })).toBe(5);
    expect(maxStackFrom({ max_stack: 25 })).toBe(25);
    expect(maxStackFrom({ limits: { max_stack: 2 } })).toBe(2);
    expect(maxStackFrom({ skills: [] })).toBe(3);
    expect(maxStackFrom({ max_stack: 0 })).toBe(3);
  });

  it("groupSkills：釘選、最近、我的、單位、全院，每個只出現一次", () => {
    const groups = groupSkills(SKILLS);
    expect(groups.map((g) => [g.key, g.items.map((s) => s.id)])).toEqual([
      ["pinned", [7]],
      ["recent", [8]],
      ["mine", [11]],
      ["unit", [9]],
      ["campus", [10]],
    ]);
  });

  it("applied skills：新格式、舊格式、落庫與讀回", () => {
    const event = {
      id: 7, name: "週報", mode: "manual",
      applied_skill: { id: 7, name: "週報", mode: "manual" },
      applied_skills: [{ id: 7, name: "週報", body: "a", mode: "manual" }, { id: 9, name: "公文", body: "b", mode: "manual" }],
    };
    expect(appliedSkillList(event).map((s) => s.name)).toEqual(["週報", "公文"]);
    expect(appliedSkillList({ id: 7, name: "舊的", mode: "auto" }).map((s) => s.name)).toEqual(["舊的"]);
    const meta = buildPersistMeta(null, { appliedSkill: event });
    expect(meta.applied_skill.name).toBe("週報");
    expect(meta.applied_skills.map((s) => s.id)).toEqual([7, 9]);
    expect(appliedSkillList(appliedSkillFromMeta(meta)).map((s) => s.id)).toEqual([7, 9]);
    expect(appliedSkillFromMeta({ applied_skill: { id: 1, name: "只有舊欄位" } }).name).toBe("只有舊欄位");
  });

  it("重載的訊息讀得到多個 skill", () => {
    const msg = mapServerMessage({
      id: 5, role: "assistant", content: "好",
      metadata: { applied_skill: { id: 7, name: "週報", mode: "manual" }, applied_skills: [{ id: 7, name: "週報", mode: "manual" }, { id: 8, name: "翻譯", mode: "manual" }] },
    });
    expect(appliedSkillList(msg.appliedSkill)).toHaveLength(2);
  });
});

describe("Composer：多個 skill chip", () => {
  it("選兩個 → 兩個 chip，送出帶 skillIds（順序）與 skillId（第一個）", () => {
    const { onSend, ta } = renderComposer();
    openMenu();
    fireEvent.click(screen.getByRole("option", { name: /週報/ }));
    typeAtEnd(ta, "/翻");
    fireEvent.click(screen.getByRole("option", { name: /翻譯/ }));
    expect(screen.getAllByTestId("skill-chip").map((c) => c.textContent)).toEqual(["套用：週報", "套用：翻譯"]);
    expect(ta.value).toBe("");
    typeAtEnd(ta, "請處理");
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    const meta = onSend.mock.calls[0][2];
    expect(meta.skillIds).toEqual([7, 8]);
    expect(meta.skillId).toBe(7);
    expect(screen.queryByTestId("skill-chip")).toBeNull();
  });

  it("達到上限：其他選項停用、提示上限，不會再加", () => {
    renderComposer({ maxSkills: 2 });
    openMenu();
    fireEvent.click(screen.getByRole("option", { name: /週報/ }));
    openMenu();
    fireEvent.click(screen.getByRole("option", { name: /翻譯/ }));
    expect(screen.getByText(/已達上限/)).toBeTruthy();
    openMenu();
    const third = screen.getByRole("option", { name: /公文/ });
    expect(third.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(third);
    expect(screen.getAllByTestId("skill-chip")).toHaveLength(2);
  });

  it("chip 的 × 移除那一個；游標在最前面按 Backspace 移除最後一個", () => {
    const { ta } = renderComposer();
    openMenu();
    fireEvent.click(screen.getByRole("option", { name: /週報/ }));
    openMenu();
    fireEvent.click(screen.getByRole("option", { name: /翻譯/ }));
    openMenu();
    fireEvent.click(screen.getByRole("option", { name: /公文/ }));
    fireEvent.click(screen.getByRole("button", { name: "移除 skill：週報" }));
    expect(screen.getAllByTestId("skill-chip").map((c) => c.textContent)).toEqual(["套用：翻譯", "套用：公文"]);
    Object.defineProperty(ta, "selectionStart", { configurable: true, get: () => 0 });
    Object.defineProperty(ta, "selectionEnd", { configurable: true, get: () => 0 });
    fireEvent.keyDown(ta, { key: "Backspace" });
    expect(screen.getAllByTestId("skill-chip").map((c) => c.textContent)).toEqual(["套用：翻譯"]);
  });

  it("選單分組顯示，星號呼叫 onToggleSkillFavorite", () => {
    const onToggleSkillFavorite = vi.fn();
    renderComposer({ onToggleSkillFavorite });
    openMenu();
    const picker = screen.getByTestId("skill-picker");
    expect(within(picker).getByRole("group", { name: "釘選" })).toBeTruthy();
    expect(within(picker).getByRole("group", { name: "最近用過" })).toBeTruthy();
    expect(within(picker).getByRole("group", { name: "單位" })).toBeTruthy();
    expect(within(picker).getByRole("group", { name: "全院" })).toBeTruthy();
    const star = screen.getByRole("button", { name: "取消釘選 週報" });
    expect(star.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "釘選 公文" }));
    expect(onToggleSkillFavorite).toHaveBeenCalledWith(expect.objectContaining({ id: 9 }));
  });
});

describe("訊息上的多技能指示", () => {
  it("顯示全部名稱、數量，展開看得到每一份內容", () => {
    render(
      <MessageBubble
        msg={{
          id: "a1", role: "assistant", text: "好",
          appliedSkill: {
            id: 7, name: "週報", mode: "auto",
            applied_skills: [{ id: 7, name: "週報", body: "甲", mode: "auto" }, { id: 9, name: "公文", body: "乙", mode: "auto" }],
          },
        }}
        agents={[]}
      />,
    );
    const mark = screen.getByTestId("applied-skill");
    expect(mark.textContent).toContain("週報、公文");
    expect(mark.textContent).toContain("×2");
    fireEvent.click(within(mark).getByRole("button"));
    expect(screen.getAllByTestId("applied-skill-body").map((n) => n.textContent)).toEqual(["甲", "乙"]);
  });

  it("舊對話只有 applied_skill 也照樣顯示", () => {
    render(
      <MessageBubble msg={{ id: "a2", role: "assistant", text: "好", appliedSkill: { id: 7, name: "週報", body: "甲", mode: "auto" } }} agents={[]} />,
    );
    expect(screen.getByTestId("applied-skill").textContent).toContain("已自動套用：週報");
  });
});

describe("SkillManager：釘選、複製成我的、使用次數", () => {
  it("單位與全院 skill 可以釘選與複製", async () => {
    const calls = [];
    const authRequest = vi.fn(async (path, opts = {}) => {
      calls.push([opts.method || "GET", path]);
      if (path === "/api/skills?view=mine") return { skills: [{ id: 7, name: "週報", description: "d", scope: "personal", status: "published", owner_user_id: 1 }] };
      if (path === "/api/skills?view=usable") {
        return {
          skills: [
            { id: 7, name: "週報", description: "d", scope: "personal", owner_user_id: 1, use_count_30d: 4, favorite: false },
            { id: 9, name: "公文", description: "改成公文格式", scope: "unit", owner_user_id: 2, use_count_30d: 0, favorite: false },
          ],
        };
      }
      if (path === "/api/skills/publish-targets") return { campus: false, units: [], submit_units: [] };
      return {};
    });
    render(
      <ConfirmProvider>
        <SkillManager authRequest={authRequest} user={{ id: 1 }} />
      </ConfirmProvider>,
    );
    const shared = await screen.findByTestId("shared-skills");
    expect(screen.getByText("30 天 4 次")).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(shared).getByRole("button", { name: "釘選 公文" }));
    });
    expect(calls).toContainEqual(["POST", "/api/skills/9/favorite"]);
    expect(within(shared).getByRole("button", { name: "取消釘選 公文" }).getAttribute("aria-pressed")).toBe("true");
    await act(async () => {
      fireEvent.click(within(shared).getByRole("button", { name: "複製「公文」成我的 skill" }));
    });
    expect(calls).toContainEqual(["POST", "/api/skills/9/copy"]);
  });
});

describe("串流標頭：整條路", () => {
  it("選兩個 skill 送出，chat 請求帶 X-ANILA-Skill-Ids；落庫的 meta 有兩個", async () => {
    const backend = createFakeBackend();
    backend.route("GET", "/api/skills", (_req, { jsonResponse }) => jsonResponse({
      skills: [
        { id: 7, name: "週報", description: "整理本週重點", status: "published", scope: "personal" },
        { id: 8, name: "翻譯", description: "翻成英文", status: "published", scope: "personal" },
      ],
      max_stack: 2,
    }));
    const applied = [{ id: 7, name: "週報", body: "甲", mode: "manual" }, { id: 8, name: "翻譯", body: "乙", mode: "manual" }];
    backend.enqueueFrames([
      namedEventFrame("anila.skill", { ...applied[0], applied_skill: applied[0], applied_skills: applied }),
      ...scriptAnswer("疊加後的回答"),
    ]);
    await mountOrchestrator({ backend });
    const box = screen.getByRole("textbox", { name: "傳訊息給 ANILA" });
    for (const name of [/週報/, /翻譯/]) {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "skill" })); });
      const option = await screen.findByRole("option", { name });
      await act(async () => { fireEvent.click(option); });
    }
    await act(async () => { fireEvent.change(box, { target: { value: "請處理" } }); });
    await act(async () => { fireEvent.click(screen.getByLabelText("送出")); });
    await screen.findByText("疊加後的回答");
    const stream = backend.requests.find((req) => req.path === "/v1/chat/completions" && req.body?.stream !== false);
    expect(stream.init.headers["X-ANILA-Skill-Ids"]).toBe("7,8");
    expect(stream.init.headers["X-ANILA-Skill-Id"]).toBe("7");
    // 手動選的 skill 在一般對話不另外標示（輸入框已經看過 chip）；但要完整落庫。
    await waitFor(() => {
      const saved = backend.requests.filter((req) => Array.isArray(req.body?.metadata?.applied_skills));
      expect(saved.length).toBeGreaterThan(0);
      expect(saved.at(-1).body.metadata.applied_skills.map((s) => s.id)).toEqual([7, 8]);
    });
  });
});

describe("並排比較也用治理中心的疊加上限", () => {
  it("ParallelCompareView 把 maxSkills / onToggleSkillFavorite 傳給 Composer", async () => {
    const { ParallelCompareView } = await import("../multiagent.jsx");
    const seen = {};
    const FakeComposer = (props) => { Object.assign(seen, props); return null; };
    const fav = () => {};
    render(
      <ParallelCompareView
        agents={[{ id: "a", name: "A" }, { id: "b", name: "B" }]}
        columns={[{ id: "c1", agentId: "a" }, { id: "c2", agentId: "b" }]}
        setColumns={() => {}} messagesByColumn={{}} onSend={() => {}} onExit={() => {}} onAdoptColumn={() => {}}
        AgentSelector={() => null} Composer={FakeComposer} MessageBubble={() => null}
        maxSkills={2} onToggleSkillFavorite={fav}
      />,
    );
    expect(seen.maxSkills).toBe(2);
    expect(seen.onToggleSkillFavorite).toBe(fav);
  });
});
