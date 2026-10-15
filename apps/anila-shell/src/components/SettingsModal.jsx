// Settings modal (general / memory / skills / about tabs). Split out of app.jsx.

import React from "react";
import { MemoryTab } from "../memory.jsx";
import { SkillManager } from "../skills.jsx";
import { Modal } from "../components.jsx";
import { AnilaLogoImg } from "../AnilaBrand.jsx";
import {
  IconHistory,
  IconBook,
  IconSettings,
  IconShield,
  IconUser,
} from "../icons.jsx";
import { REDACTION_MODES } from "../trust.jsx";
import { FOLLOW_UPS_UNAVAILABLE, FOLLOW_UPS_UNAVAILABLE_MESSAGE } from '../runtime/followUps.js';

export function SettingsModal({
  open, tab, setTab, onClose,
  user, agents, authRequest,
  redactionMode, onChangeRedactionMode,
  skillAutoApply, onSkillAutoApplyChange, onSkillsChanged,
  followUpsEnabled = false, followUpsAvailable = false, onFollowUpsEnabledChange,
  onOpenConversation,
  settingsLoadError, settingsUnsaved, onRetrySettingsLoad,
}) {
  return (
    <Modal open={open} onClose={onClose} title="設定" subtitle="顯示、隱私與帳號" width={680}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, paddingBottom: 8, borderBottom: "1px solid var(--border)" }}>
          {[
            { id: "general", label: "一般",       icon: <IconSettings size={13} /> },
            { id: "privacy", label: "隱私 / 信任", icon: <IconShield   size={13} /> },
            { id: "memory",  label: "記憶",        icon: <IconHistory  size={13} /> },
            { id: "skills",  label: "我的 skill",  icon: <IconBook     size={13} /> },
            { id: "account", label: "帳號",        icon: <IconUser     size={13} /> },
            { id: "about",   label: "關於",        icon: <AnilaLogoImg variant="mark" height={13} /> },
          ].map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)} style={{
              display: "flex", alignItems: "center", gap: 8,
              padding: "7px 10px", fontSize: 13,
              background: tab === t.id ? "var(--bg-subtle)" : "transparent",
              border: "1px solid " + (tab === t.id ? "var(--border)" : "transparent"),
              borderRadius: "var(--radius)",
              color: "var(--fg)", textAlign: "left", cursor: "pointer",
            }}>
              {t.icon}{t.label}
            </button>
          ))}
        </div>
        {(settingsLoadError || settingsUnsaved) ? (
          <div
            role="status"
            style={{
              display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
              padding: "8px 10px", fontSize: 12, lineHeight: 1.5,
              background: "var(--bg-subtle)", border: "1px solid var(--border)",
              borderRadius: "var(--radius)", color: "var(--fg)",
            }}
          >
            <span>
              {settingsUnsaved
                ? "這次修改還沒存到伺服器。"
                : "設定暫時讀不到伺服器。修改可能還沒保存。"}
            </span>
            {onRetrySettingsLoad ? (
              <button
                type="button"
                onClick={onRetrySettingsLoad}
                style={{
                  flexShrink: 0, padding: "4px 10px", fontSize: 12, cursor: "pointer",
                  background: "var(--bg-elev)", border: "1px solid var(--border-strong)",
                  borderRadius: 4, color: "var(--fg)",
                }}
              >
                重試
              </button>
            ) : null}
          </div>
        ) : null}
        <div>
          {tab === "general" && (
            <div style={{ display: "grid", gap: 12 }}>
              <div>
                <div style={{ fontSize: 13, fontWeight: 500 }}>預設助手</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 4 }}>
                  目前有 {Math.max(agents.length - 1, 0)} 個可用助手，請從主畫面的助手選單切換。
                </div>
              </div>
              <div data-testid="follow-ups-setting">
                <div style={{ fontSize: 13, fontWeight: 500 }} id="follow-ups-setting-title">回答後顯示建議問題</div>
                {followUpsAvailable !== true ? <p role="status" style={{ fontSize: 12, color: "var(--fg-muted)" }}>{FOLLOW_UPS_UNAVAILABLE_MESSAGE}（{FOLLOW_UPS_UNAVAILABLE}）</p> : <>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 4, lineHeight: 1.6 }}>
                  每則回答結束後，在下方列出兩三個你可能想接著問的問題；點一下會帶進輸入框，不會直接送出。
                  輸入框空著時也會用灰字預覽第一個，按 Tab 採用。關掉後就不再產生。
                </div>
                <div role="group" aria-labelledby="follow-ups-setting-title" style={{ display: "flex", gap: 6, marginTop: 8 }}>
                  {[
                    { on: true, label: "顯示建議問題" },
                    { on: false, label: "不要顯示" },
                  ].map((opt) => {
                    const active = (followUpsEnabled !== false) === opt.on;
                    return (
                      <button
                        key={String(opt.on)}
                        type="button"
                        aria-pressed={active}
                        onClick={() => onFollowUpsEnabledChange?.(opt.on)}
                        style={{
                          padding: "4px 10px", fontSize: 11,
                          background: active ? "var(--bg-subtle)" : "transparent",
                          border: "1px solid " + (active ? "var(--border-strong)" : "var(--border)"),
                          borderRadius: 4, cursor: "pointer", color: "var(--fg)",
                        }}
                      >{opt.label}</button>
                    );
                  })}
                </div>
                </>}
              </div>
              <div style={{ fontSize: 12, color: "var(--fg-muted)" }}>
                列管等級由助手的預設分類等級（無機密／營業秘密／密／機密）決定；對話一旦升至較高等級即單向鎖定，使用者無法手動切換或降級。
              </div>
            </div>
          )}

          {/* Sprint 7 X follow-up：apikey tab 已下線 — SPA 不再持有 key。 */}

          {tab === "privacy" && (
            <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
              <div>
                <div style={{ fontWeight: 500, marginBottom: 4 }}>敏感資訊處理</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
                  平台只負責提醒你草稿裡可能有什麼，不會替你改寫內容：送給模型的文字、以及存下來的對話紀錄，都是你打的原文（只去掉頭尾的空白），中間一字不改。送出去之後就收不回來，要不要送由你決定。
                </div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6, marginTop: 6 }}>
                  下面選的模式會存在你的帳號下，換一台機器登入同一張卡也會保留。這是你自己的偏好，沒有管理員替你設定過，你隨時可以改回來。
                </div>
                {/* ⚠ 這組按鈕是這個設定在整個平台上**唯一永遠到得了**的入口。
                    在它之前,唯一能改模式的地方是輸入框上方那條提示列 —— 而那條
                    提示列只在草稿裡剛好偵測到個資時才出現。於是被「阻擋」擋下來的
                    人(尤其是從範本或重試被擋的,輸入框根本是空的)看不到任何開關,
                    而那個偏好現在還是跨機器長期保存的:幾週前在別台機器設的,
                    今天在這裡把自己鎖在門外。擋人的控制項一定要有一條自救的路。 */}
                <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                  {REDACTION_MODES.map((m) => (
                    <button
                      key={m}
                      onClick={() => onChangeRedactionMode?.(m)}
                      aria-pressed={redactionMode === m}
                      style={{
                        padding: "4px 10px",
                        fontSize: 11, fontFamily: "var(--font-mono)",
                        background: redactionMode === m ? "var(--bg-subtle)" : "transparent",
                        border: "1px solid " + (redactionMode === m ? "var(--border-strong)" : "var(--border)"),
                        borderRadius: 4, cursor: "pointer", color: "var(--fg)",
                      }}
                    >{m === "warn" ? "偵測個資時提醒" : "偵測個資時阻止送出"}</button>
                  ))}
                </div>
                <div style={{ fontSize: 10, color: "var(--fg-subtle)", lineHeight: 1.6, marginTop: 6 }}>
                  偵測只認得它知道的那幾種形狀（身分證、電話、Email、信用卡、
                  API 金鑰、權杖、私密金鑰、密碼），認不出來的不代表沒有，認出來的也可能認錯。
                  「偵測個資時提醒」會先告訴你再照樣送出；「偵測個資時阻止送出」只攔個資。
                  金鑰、權杖、密碼仍只提醒、不會擋送出。
                </div>
              </div>
              <div>
                <div style={{ fontWeight: 500, marginBottom: 4 }}>列管對話</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
                  若指定的 agent 預設分類等級高於無機密，此對話會依該等級列管（受控存取）：
                  密／機密禁止分享與匯出，營業秘密可分享但會落稽核，並加上浮水印。此狀態無法由使用者解除。
                </div>
              </div>
            </div>
          )}

          {tab === "memory" && (
            <MemoryTab
              authRequest={authRequest}
              onOpenConversation={onOpenConversation}
              userId={user?.id}
            />
          )}

          {tab === "skills" && (
            <SkillManager
              authRequest={authRequest}
              user={user}
              autoApply={skillAutoApply}
              onAutoApplyChange={onSkillAutoApplyChange}
              onChanged={onSkillsChanged}
            />
          )}

          {tab === "account" && (
            <div style={{ fontSize: 13 }}>
              <div style={{ marginBottom: 4 }}><b>{user?.username}</b></div>
              <div style={{ color: "var(--fg-muted)", fontSize: 12 }}>
                角色：{user?.role || "user"}
              </div>
            </div>
          )}

          {tab === "about" && (
            <div style={{ fontSize: 13, lineHeight: 1.7 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                <AnilaLogoImg variant="logo" height={32} />
                <div style={{ fontSize: 16, fontWeight: 600 }}>ANILA Runtime Client</div>
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--fg-muted)" }}>
                v{typeof __ANILA_VERSION__ !== "undefined" ? __ANILA_VERSION__ : "dev"} · ANILA 對話介面
              </div>
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
