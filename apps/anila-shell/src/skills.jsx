// 我的 skill：設定頁管理，以及對話裡的套用標示。
import React, { useCallback, useEffect, useState } from "react";

import { Button } from "./components.jsx";
import { useConfirm, useToast } from "./confirm.jsx";
import { IconBook, IconChevDown, IconChevRight, IconPencil, IconPlus, IconTrash, IconX } from "./icons.jsx";
import {
  SKILL_STATUS_LABEL,
  appliedSkillLabel,
  createSkill,
  deleteSkill,
  listPublishTargets,
  listSkills,
  skillChipLabel,
  submitSkill,
  updateSkill,
} from "./runtime/skills.js";

const EMPTY_FORM = {
  name: "",
  description: "",
  body: "",
  auto_apply: false,
};

// 設定頁其他分頁的排版：區塊標題 13px/500、說明 11px muted、選項用膠囊按鈕。
const SECTION_TITLE = { fontSize: 13, fontWeight: 500, marginBottom: 4 };
const SECTION_HINT = { fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 };
const FIELD_LABEL = { fontSize: 12, fontWeight: 500, color: "var(--fg-muted)", marginBottom: 6, display: "flex", justifyContent: "space-between" };
const FIELD_BOX = {
  width: "100%", boxSizing: "border-box",
  background: "var(--bg-elev)", border: "1px solid var(--border)",
  borderRadius: "var(--radius)", padding: "8px 10px",
  fontSize: 13, color: "var(--fg)", fontFamily: "inherit", outline: "none",
};
const COUNT = { fontWeight: 400, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)", fontSize: 10 };

function pillStyle(active) {
  return {
    padding: "4px 10px", fontSize: 11,
    background: active ? "var(--bg-subtle)" : "transparent",
    border: "1px solid " + (active ? "var(--border-strong)" : "var(--border)"),
    borderRadius: 4, cursor: "pointer", color: "var(--fg)",
  };
}

const STATUS_TONE = {
  draft: { color: "var(--fg-muted)", border: "var(--border)" },
  pending: { color: "var(--warn, var(--fg))", border: "var(--border-strong)" },
  published: { color: "var(--accent)", border: "var(--accent)" },
  rejected: { color: "var(--danger)", border: "var(--danger)" },
  unpublished: { color: "var(--fg-subtle)", border: "var(--border)" },
};

function StatusBadge({ status }) {
  const tone = STATUS_TONE[status] || STATUS_TONE.draft;
  return (
    <span style={{
      flexShrink: 0, fontSize: 10, fontFamily: "var(--font-mono)",
      padding: "1px 6px", borderRadius: 999,
      border: `1px solid ${tone.border}`, color: tone.color,
    }}>
      {SKILL_STATUS_LABEL[status] || status}
    </span>
  );
}

function scopeLabel(row) {
  if (row.scope === "campus") return "全院";
  if (row.scope === "unit") return row.department_name ? `單位：${row.department_name}` : "單位";
  return "個人";
}

const chipButton = {
  display: "inline-flex", alignItems: "center", justifyContent: "center",
  width: 16, height: 16, padding: 0, border: "none", borderRadius: 999,
  background: "transparent", color: "var(--fg-muted)", cursor: "pointer",
};

export function SkillChip({ skill, onRemove }) {
  if (!skill?.name) return null;
  return (
    <div
      data-testid="skill-chip"
      style={{
        display: "inline-flex", alignItems: "center", gap: 6,
        margin: "8px 12px 0", padding: "2px 4px 2px 8px",
        border: "1px solid var(--border)", borderRadius: 999,
        fontSize: 12, color: "var(--fg)", background: "var(--bg-subtle)",
        alignSelf: "flex-start",
      }}
    >
      <IconBook size={12} />
      <span>{skillChipLabel(skill.name)}</span>
      <button type="button" aria-label="移除 skill" title="移除 skill" onClick={onRemove} style={chipButton}>
        <IconX size={11} />
      </button>
    </div>
  );
}

export function AppliedSkillIndicator({ skill, includeManual = false }) {
  const [open, setOpen] = useState(false);
  if (!skill?.name) return null;
  const showManual = includeManual && skill.mode === "manual";
  const showAuto = skill.mode === "auto";
  if (!showManual && !showAuto) return null;
  const label = showManual ? skillChipLabel(skill.name) : appliedSkillLabel(skill.name);
  return (
    <div data-testid="applied-skill" style={{ marginBottom: 8, fontSize: 11 }}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        style={{
          display: "inline-flex", alignItems: "center", gap: 5,
          padding: "2px 8px", borderRadius: 999,
          border: "1px solid var(--border)", background: "transparent",
          color: "var(--fg-muted)", fontSize: 11, cursor: "pointer",
        }}
      >
        <IconBook size={11} />
        {label}
        {open ? <IconChevDown size={10} /> : <IconChevRight size={10} />}
      </button>
      {open ? (
        <pre
          data-testid="applied-skill-body"
          style={{
            whiteSpace: "pre-wrap", margin: "6px 0 0", padding: "8px 10px",
            background: "var(--bg-subtle)", border: "1px solid var(--border)",
            borderRadius: "var(--radius)", fontSize: 12, lineHeight: 1.6,
            fontFamily: "inherit", color: "var(--fg)",
          }}
        >
          {skill.body || ""}
        </pre>
      ) : null}
    </div>
  );
}

function Field({ label, count, max, hint, children }) {
  return (
    <label style={{ display: "block" }}>
      <div style={FIELD_LABEL}>
        <span>{label}</span>
        {max ? <span style={COUNT}>{count}/{max}</span> : null}
      </div>
      {children}
      {hint ? <div style={{ fontSize: 11, color: "var(--fg-subtle)", marginTop: 4, lineHeight: 1.5 }}>{hint}</div> : null}
    </label>
  );
}

export function SkillManager({
  authRequest,
  user,
  autoApply = true,
  onAutoApplyChange,
  onChanged,
}) {
  const confirm = useConfirm();
  const toast = useToast();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editingId, setEditingId] = useState(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [publishTargets, setPublishTargets] = useState({ campus: false, units: [], submit_units: [] });
  const [publishTo, setPublishTo] = useState("");
  const directPublish = publishTargets.campus || publishTargets.units.length > 0;
  const submitUnits = publishTargets.submit_units || [];
  const ownSubmitUnit = submitUnits.find((unit) => unit.id === user?.department_id) || null;

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await listSkills(authRequest, "mine"));
    } catch (err) {
      toast(err?.message || "讀取 skill 失敗", { tone: "error" });
    } finally {
      setLoading(false);
    }
  }, [authRequest, toast]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listPublishTargets(authRequest);
        if (cancelled || !data || typeof data !== "object") return;
        setPublishTargets({
          campus: Boolean(data.campus),
          units: Array.isArray(data.units) ? data.units : [],
          submit_units: Array.isArray(data.submit_units) ? data.submit_units : [],
        });
      } catch {
        if (!cancelled) setPublishTargets({ campus: false, units: [], submit_units: [] });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authRequest]);

  const reset = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setPublishTo("");
    setEditorOpen(false);
  };

  const openNew = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setPublishTo("");
    setEditorOpen(true);
  };

  const openEdit = (row) => {
    setEditingId(row.id);
    setForm({
      name: row.name,
      description: row.description,
      body: row.body,
      auto_apply: Boolean(row.auto_apply),
    });
    setPublishTo("");
    setEditorOpen(true);
  };

  const canSave = form.name.trim() && form.description.trim() && form.body.trim();

  const save = async () => {
    if (!canSave) return;
    setBusy(true);
    try {
      const payload = {
        name: form.name,
        description: form.description,
        body: form.body,
        auto_apply: Boolean(form.auto_apply),
      };
      if (editingId) {
        await updateSkill(authRequest, editingId, payload);
      } else {
        const unitId = publishTo.startsWith("unit:") ? Number(publishTo.slice(5)) : null;
        const scope = publishTo === "campus" ? "campus" : unitId ? "unit" : "personal";
        await createSkill(authRequest, {
          ...payload,
          scope,
          department_id: scope === "unit" ? unitId : null,
          submit: !directPublish && scope === "unit",
        });
      }
      const wasEditing = Boolean(editingId);
      reset();
      await reload();
      onChanged?.();
      toast(wasEditing ? "已儲存" : "已建立", { tone: "success" });
    } catch (err) {
      toast(err?.message || "儲存失敗", { tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row) => {
    const ok = await confirm({
      title: "刪除 skill",
      message: `刪除「${row.name}」？已發布的要先下架。`,
      confirmText: "刪除",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await deleteSkill(authRequest, row.id);
      await reload();
      onChanged?.();
    } catch (err) {
      toast(err?.message || "刪除失敗", { tone: "error" });
    }
  };

  const submit = async (row, scope) => {
    const departmentId = user?.department_id;
    if (scope === "unit" && !departmentId) {
      toast("帳號沒有單位，無法送審到單位", { tone: "error" });
      return;
    }
    try {
      await submitSkill(authRequest, row.id, {
        scope,
        department_id: scope === "unit" ? departmentId : null,
      });
      await reload();
      onChanged?.();
      toast(scope === "unit" ? "已送單位審核" : "已送全院審核", { tone: "success" });
    } catch (err) {
      toast(err?.message || "送審失敗", { tone: "error" });
    }
  };

  const saveLabel = editingId
    ? "儲存修改"
    : publishTo
      ? (directPublish ? "直接發布" : "送審到單位")
      : "建立個人 skill";

  const editor = (
    <div style={{
      display: "grid", gap: 12, padding: 14,
      background: "var(--bg-subtle)", border: "1px solid var(--border)",
      borderRadius: "var(--radius)",
    }}>
      <div style={{ ...SECTION_TITLE, marginBottom: 0 }}>{editingId ? "編輯 skill" : "新增 skill"}</div>
      <Field label="名稱" count={form.name.length} max={40}>
        <input
          aria-label="skill 名稱"
          value={form.name}
          maxLength={40}
          placeholder="例如：週報整理"
          style={FIELD_BOX}
          onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
        />
      </Field>
      <Field
        label="用途說明"
        count={form.description.length}
        max={200}
        hint="一句話說明這個 skill 做什麼、什麼時候用。自動套用時，模型只看這段判斷要不要用。"
      >
        <input
          aria-label="skill 用途"
          value={form.description}
          maxLength={200}
          placeholder="例如：把一週的工作紀錄整理成三段式週報"
          style={FIELD_BOX}
          onChange={(event) => setForm((prev) => ({ ...prev, description: event.target.value }))}
        />
      </Field>
      <Field
        label="內容"
        count={form.body.length}
        max={8000}
        hint="給模型的指示：步驟、格式、範例。平台只把這段文字交給模型，不會執行它。"
      >
        <textarea
          aria-label="skill 內容"
          value={form.body}
          maxLength={8000}
          rows={8}
          placeholder={"例如：\n1. 先用一句話總結本週進度\n2. 列出完成事項、進行中、下週計畫三段\n3. 每段不超過五點"}
          style={{ ...FIELD_BOX, resize: "vertical", lineHeight: 1.6, minHeight: 140 }}
          onChange={(event) => setForm((prev) => ({ ...prev, body: event.target.value }))}
        />
      </Field>
      <div>
        <div style={FIELD_LABEL}><span>套用方式</span></div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          <button
            type="button"
            aria-pressed={!form.auto_apply}
            style={pillStyle(!form.auto_apply)}
            onClick={() => setForm((prev) => ({ ...prev, auto_apply: false }))}
          >只在手動選擇時</button>
          <button
            type="button"
            aria-pressed={Boolean(form.auto_apply)}
            style={pillStyle(Boolean(form.auto_apply))}
            onClick={() => setForm((prev) => ({ ...prev, auto_apply: true }))}
          >也讓模型自動判斷</button>
        </div>
      </div>
      {!editingId && directPublish ? (
        <Field label="發布範圍" hint="直接發布不經審核，對象單位的同仁馬上可以使用。">
          <select
            aria-label="直接發布到…"
            value={publishTo}
            style={FIELD_BOX}
            onChange={(event) => setPublishTo(event.target.value)}
          >
            <option value="">只有我（個人 skill）</option>
            {publishTargets.units.map((unit) => (
              <option key={unit.id} value={`unit:${unit.id}`}>{unit.name}</option>
            ))}
            {publishTargets.campus ? <option value="campus">全院</option> : null}
          </select>
        </Field>
      ) : null}
      {!editingId && !directPublish && submitUnits.length > 0 ? (
        <Field label="分享給單位" hint="送審後由單位管理員核准，核准前只有你自己能用。">
          <select
            aria-label="送審到單位"
            value={publishTo}
            style={FIELD_BOX}
            onChange={(event) => setPublishTo(event.target.value)}
          >
            <option value="">不送審，先存成個人 skill</option>
            {submitUnits.map((unit) => (
              <option key={unit.id} value={`unit:${unit.id}`}>{unit.name}</option>
            ))}
          </select>
        </Field>
      ) : null}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" size="sm" type="button" onClick={reset}>取消</Button>
        <Button
          variant="primary"
          size="sm"
          type="button"
          disabled={busy || !canSave}
          style={{ opacity: busy || !canSave ? 0.55 : 1, cursor: busy || !canSave ? "not-allowed" : "pointer" }}
          onClick={save}
        >
          {saveLabel}
        </Button>
      </div>
    </div>
  );

  return (
    <div style={{ display: "grid", gap: 18, fontSize: 13 }}>
      <div>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
          <div>
            <div style={SECTION_TITLE}>我的 skill</div>
            <div style={SECTION_HINT}>
              把常用的指示存成 skill，對話時在輸入框打「/」或按 <span style={{ display: "inline-flex", verticalAlign: -1 }}><IconBook size={11} /></span> 選用。
              skill 只是文字，平台不會執行裡面的任何東西。
            </div>
          </div>
          {!editorOpen ? (
            <Button variant="default" size="sm" type="button" leftIcon={<IconPlus size={12} />} onClick={openNew}>
              新增 skill
            </Button>
          ) : null}
        </div>
      </div>

      {editorOpen ? editor : null}

      <div>
        {loading ? <div style={SECTION_HINT}>讀取中…</div> : null}
        {!loading && rows.length === 0 && !editorOpen ? (
          <div style={{
            padding: "18px 12px", textAlign: "center", ...SECTION_HINT,
            border: "1px dashed var(--border)", borderRadius: "var(--radius)",
          }}>還沒有 skill。</div>
        ) : null}
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
          {rows.map((row) => {
            const editable = row.status !== "pending";
            const shareable = row.scope === "personal" && (row.status === "draft" || row.status === "rejected");
            return (
              <li
                key={row.id}
                style={{
                  border: "1px solid var(--border)", borderRadius: "var(--radius)",
                  padding: "10px 12px", background: editingId === row.id ? "var(--bg-subtle)" : "var(--bg-elev)",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ display: "inline-flex", color: "var(--fg-muted)", flexShrink: 0 }}><IconBook size={13} /></span>
                  <span style={{ fontWeight: 500, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.name}</span>
                  <StatusBadge status={row.status} />
                  <span style={{ fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
                    {scopeLabel(row)}{row.auto_apply ? " · 可自動套用" : ""}
                  </span>
                  <span style={{ flex: 1 }} />
                  <button
                    type="button"
                    title="編輯"
                    aria-label={`編輯 ${row.name}`}
                    disabled={!editable}
                    onClick={() => openEdit(row)}
                    style={{ ...chipButton, width: 24, height: 24, opacity: editable ? 1 : 0.4 }}
                  ><IconPencil size={13} /></button>
                  <button
                    type="button"
                    title="刪除"
                    aria-label={`刪除 ${row.name}`}
                    onClick={() => remove(row)}
                    style={{ ...chipButton, width: 24, height: 24, color: "var(--danger)" }}
                  ><IconTrash size={13} /></button>
                </div>
                <div style={{ ...SECTION_HINT, marginTop: 4 }}>{row.description}</div>
                {row.reject_reason ? (
                  <div style={{ color: "var(--danger)", fontSize: 11, marginTop: 4 }}>退回理由：{row.reject_reason}</div>
                ) : null}
                {shareable ? (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                    <button type="button" style={pillStyle(false)} onClick={() => submit(row, "unit")}>
                      {ownSubmitUnit ? `送審到單位（${ownSubmitUnit.name}）` : "送審到單位"}
                    </button>
                    <button type="button" style={pillStyle(false)} onClick={() => submit(row, "campus")}>送審到全院</button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      </div>

      <div style={{ borderTop: "1px solid var(--border)", paddingTop: 14 }}>
        <div style={SECTION_TITLE}>自動套用</div>
        <div style={SECTION_HINT}>
          開啟時，若你沒有手動選 skill，模型會從「可自動套用」的 skill 裡判斷要不要用一個，回覆上方會標示「已自動套用」。
          這是你自己的偏好，關掉不影響手動選用。
        </div>
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <button
            type="button"
            aria-pressed={autoApply !== false}
            style={pillStyle(autoApply !== false)}
            onClick={() => onAutoApplyChange?.(true)}
          >開啟自動套用</button>
          <button
            type="button"
            aria-pressed={autoApply === false}
            style={pillStyle(autoApply === false)}
            onClick={() => onAutoApplyChange?.(false)}
          >只用我手動選的</button>
        </div>
      </div>
    </div>
  );
}
