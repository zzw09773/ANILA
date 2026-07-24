// 使用者標籤的上限與自動標籤保護。
//
// 為什麼要測:標籤跟 folders 一起塞在 users.ui_settings 這個整包取代的 blob,
// 後端硬上限 256KB(超過回 413)。前端沒有上限的話,使用者可以一路加到 blob
// 撐爆,之後**每一次**設定變更都失效 —— 而且過去 413 被靜默吞掉,完全看不見。
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { TagEditor } from "../collab.jsx";
import {
  AUTO_TAGS,
  MAX_TAGS_PER_CONVERSATION,
  MAX_TAG_LENGTH,
  isAutoTag,
  sanitizeUserTags,
  tagRejectionMessage,
} from "../runtime/tagRules.js";

describe("sanitizeUserTags", () => {
  it("去空白、去重、保留順序", () => {
    const { tags } = sanitizeUserTags([" 法務 ", "法務", "人資", ""]);
    expect(tags).toEqual(["法務", "人資"]);
  });

  it("剔除自動標籤(classified / compared 不進 convMeta)", () => {
    const { tags } = sanitizeUserTags(["classified", "compared", "法務"]);
    expect(tags).toEqual(["法務"]);
    for (const t of AUTO_TAGS) expect(isAutoTag(t)).toBe(true);
  });

  it("擋下超長標籤並回報", () => {
    const long = "超".repeat(MAX_TAG_LENGTH + 1);
    const { tags, rejected } = sanitizeUserTags([long, "短"]);
    expect(tags).toEqual(["短"]);
    expect(rejected.tooLong).toEqual([long]);
    expect(tagRejectionMessage(rejected)).toContain(String(MAX_TAG_LENGTH));
  });

  it("擋下超量標籤並回報", () => {
    const many = Array.from({ length: MAX_TAGS_PER_CONVERSATION + 3 }, (_, i) => `t${i}`);
    const { tags, rejected } = sanitizeUserTags(many);
    expect(tags).toHaveLength(MAX_TAGS_PER_CONVERSATION);
    expect(rejected.overflow).toHaveLength(3);
    expect(tagRejectionMessage(rejected)).toContain(String(MAX_TAGS_PER_CONVERSATION));
  });

  it("沒有被擋下的東西就沒有訊息(不吵)", () => {
    const { rejected } = sanitizeUserTags(["a", "b"]);
    expect(tagRejectionMessage(rejected)).toBe("");
  });

  it("對壞輸入是防禦性的", () => {
    expect(sanitizeUserTags(null).tags).toEqual([]);
    expect(sanitizeUserTags(undefined).tags).toEqual([]);
    expect(sanitizeUserTags([null, undefined, 3]).tags).toEqual(["3"]);
  });
});

describe("<TagEditor>", () => {
  const folders = [{ id: "usr-legal", name: "法務" }];

  function setup(conversation) {
    const onUpdate = vi.fn();
    render(
      <TagEditor
        folders={folders}
        conversation={conversation}
        onUpdate={onUpdate}
        close={() => {}}
      />,
    );
    return { onUpdate };
  }

  it("自動標籤沒有可刪除的 ×(按了也刪不掉,畫出來只會讓人以為壞了)", () => {
    setup({ id: 1, tags: ["classified", "法務"], folder: "all" });
    expect(screen.getByText("#classified")).toBeInTheDocument();
    expect(screen.queryByLabelText("移除標籤 classified")).not.toBeInTheDocument();
    expect(screen.getByLabelText("移除標籤 法務")).toBeInTheDocument();
  });

  it("顯示目前數量 / 上限", () => {
    setup({ id: 1, tags: ["classified", "法務"], folder: "all" });
    // classified 是自動標籤,不計入使用者額度。
    expect(screen.getByText(`標籤 1/${MAX_TAGS_PER_CONVERSATION}`)).toBeInTheDocument();
  });

  it("達到數量上限時停用輸入框並明示原因", () => {
    const tags = Array.from({ length: MAX_TAGS_PER_CONVERSATION }, (_, i) => `t${i}`);
    setup({ id: 1, tags, folder: "all" });
    expect(screen.getByLabelText("新增標籤")).toBeDisabled();
    expect(screen.getByRole("alert").textContent).toContain(String(MAX_TAGS_PER_CONVERSATION));
  });

  it("超長標籤被擋下並顯示錯誤,不會靜默丟掉", () => {
    const { onUpdate } = setup({ id: 1, tags: [], folder: "all" });
    const input = screen.getByLabelText("新增標籤");
    // maxLength 擋不住程式化輸入 → 元件本身也要判。
    fireEvent.change(input, { target: { value: "長".repeat(MAX_TAG_LENGTH + 1) } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onUpdate).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain(String(MAX_TAG_LENGTH));
  });

  it("正常標籤照樣加得進去", () => {
    const { onUpdate } = setup({ id: 1, tags: ["法務"], folder: "all" });
    const input = screen.getByLabelText("新增標籤");
    fireEvent.change(input, { target: { value: "人資" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onUpdate).toHaveBeenCalledWith({ tags: ["法務", "人資"] });
  });

  it("不讓使用者手動新增自動標籤", () => {
    const { onUpdate } = setup({ id: 1, tags: [], folder: "all" });
    const input = screen.getByLabelText("新增標籤");
    fireEvent.change(input, { target: { value: "classified" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onUpdate).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});
