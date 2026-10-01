// 我的 skill：設定頁管理，以及對話裡的套用標示。
import React, { useCallback, useEffect, useState } from "react";

import { useConfirm, useToast } from "./confirm.jsx";
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

export function SkillChip({ skill, onRemove }) {
  if (!skill?.name) return null;
  return (
    <div
      data-testid="skill-chip"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        margin: "8px 12px 0",
        padding: "3px 8px",
        border: "1px solid var(--border)",
        borderRadius: 999,
        fontSize: 12,
        background: "var(--bg-subtle)",
      }}
    >
      <span>{skillChipLabel(skill.name)}</span>
      <button type="button" aria-label="移除 skill" onClick={onRemove}>
        移除
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
    <div data-testid="applied-skill" style={{ marginBottom: 8, fontSize: 12 }}>
      <button type="button" onClick={() => setOpen((value) => !value)}>
        {label}
      </button>
      {open ? (
        <pre
          data-testid="applied-skill-body"
          style={{
            whiteSpace: "pre-wrap",
            margin: "6px 0 0",
            padding: 8,
            border: "1px solid var(--border)",
            borderRadius: 6,
            fontSize: 12,
          }}
        >
          {skill.body || ""}
        </pre>
      ) : null}
    </div>
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
  };

  const save = async () => {
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
      reset();
      await reload();
      onChanged?.();
      toast(editingId ? "已儲存" : "已建立", { tone: "success" });
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

  return (
    <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
      <label style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
        <input
          type="checkbox"
          checked={autoApply !== false}
          onChange={(event) => onAutoApplyChange?.(event.target.checked)}
        />
        <span>
          允許自動套用
          <span style={{ display: "block", color: "var(--fg-muted)", fontSize: 11 }}>
            關掉之後，這一則若沒有自己選 skill，就不會自動套用。手動選的仍然有效。
          </span>
        </span>
      </label>

      <div style={{ display: "grid", gap: 8 }}>
        <input
          aria-label="skill 名稱"
          value={form.name}
          maxLength={40}
          placeholder="名稱"
          onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
        />
        <input
          aria-label="skill 用途"
          value={form.description}
          maxLength={200}
          placeholder="用途說明（自動套用只看這段）"
          onChange={(event) => setForm((prev) => ({ ...prev, description: event.target.value }))}
        />
        <textarea
          aria-label="skill 內容"
          value={form.body}
          maxLength={8000}
          rows={6}
          placeholder="給模型的指示。平台不會執行這段文字。"
          onChange={(event) => setForm((prev) => ({ ...prev, body: event.target.value }))}
        />
        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={Boolean(form.auto_apply)}
            onChange={(event) => setForm((prev) => ({ ...prev, auto_apply: event.target.checked }))}
          />
          這個 skill 可以被自動套用
        </label>
        {!editingId && directPublish ? (
          <label style={{ display: "grid", gap: 4 }}>
            <span>直接發布到…</span>
            <select
              aria-label="直接發布到…"
              value={publishTo}
              onChange={(event) => setPublishTo(event.target.value)}
            >
              <option value="">不直接發布</option>
              {publishTargets.units.map((unit) => (
                <option key={unit.id} value={`unit:${unit.id}`}>{unit.name}</option>
              ))}
              {publishTargets.campus ? <option value="campus">全院</option> : null}
            </select>
          </label>
        ) : null}
        {!editingId && !directPublish && submitUnits.length > 0 ? (
          <label style={{ display: "grid", gap: 4 }}>
            <span>送審到單位</span>
            <select
              aria-label="送審到單位"
              value={publishTo}
              onChange={(event) => setPublishTo(event.target.value)}
            >
              <option value="">不送審，先存成個人 skill</option>
              {submitUnits.map((unit) => (
                <option key={unit.id} value={`unit:${unit.id}`}>{unit.name}</option>
              ))}
            </select>
          </label>
        ) : null}
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" disabled={busy} onClick={save}>
            {!editingId && publishTo ? (directPublish ? "直接發布" : "送審到單位") : editingId ? "儲存修改" : "建立個人 skill"}
          </button>
          {editingId ? (
            <button type="button" onClick={reset}>取消</button>
          ) : null}
        </div>
      </div>

      {loading ? <div>讀取中…</div> : null}
      {!loading && rows.length === 0 ? <div>還沒有 skill。</div> : null}
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
        {rows.map((row) => (
          <li key={row.id} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
              <strong>{row.name}</strong>
              <span>{SKILL_STATUS_LABEL[row.status] || row.status}</span>
            </div>
            <div style={{ color: "var(--fg-muted)", fontSize: 12 }}>{row.description}</div>
            {row.reject_reason ? (
              <div style={{ color: "var(--danger)", fontSize: 12 }}>退回理由：{row.reject_reason}</div>
            ) : null}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 6 }}>
              <button
                type="button"
                onClick={() => {
                  setEditingId(row.id);
                  setForm({
                    name: row.name,
                    description: row.description,
                    body: row.body,
                    auto_apply: Boolean(row.auto_apply),
                  });
                }}
              >
                編輯
              </button>
              <button type="button" onClick={() => remove(row)}>刪除</button>
              <button type="button" onClick={() => submit(row, "unit")}>
                {ownSubmitUnit ? `送審到單位（${ownSubmitUnit.name}）` : "送審到單位"}
              </button>
              <button type="button" onClick={() => submit(row, "campus")}>送審到全院</button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
