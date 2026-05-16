# Onyx Handover — Out of Monorepo

**Date:** 2026-04-27
**Operator:** kunggemini09773
**Affected refs:** all branches + tags
**Reversibility:** local backup tag `pre-onyx-filter-repo-2026-04-27` (delete after ~14 days when remote is settled)

---

## 變更摘要

| | Before | After |
|---|---|---|
| `onyx/` 路徑 | 4690 tracked files (61 MB on disk) | 移出版控；本機檔案已由作者刪除 |
| `.git` size | 34 MB | 5.7 MB (**-83%**) |
| `.gitignore` | — | 新增 `onyx/` 條目（避免未來誤 add）|
| README handoff note | 無 | 加入 2026-04-27 條目 + 顯示已移出 |
| Handover specs | `docs/archive/onyx-application-plan.md` + `docs/archive/onyx-target-system-api-spec.md` | 保留 |

---

## 為什麼搬離

1. **責任邊界**：Onyx upstream 的功能維護由 agent 開發團隊負責 — 不是我們 ANILA 平台組的活。混在同一個 repo 會讓 PR review、git blame、issue triage 都模糊。
2. **Repo 健康**：4690 個檔造成全文件搜尋（`grep -r`）每次都會掃 61 MB 不相干的 source；CI clone 與 dev fetch 也都被拖慢。
3. **整潔的 diff history**：未來如果在 README 或 docs 改 anila 自己的東西，`git log --stat` 就不會被 Onyx 的 vendoring commit 蓋掉。
4. **License 邊界**：Onyx 走自己的授權（MIT 或 Apache 視 release，看 `onyx/LICENSE`）。把它從我們 repo 拔出能避免授權混淆 — 我們不再「事實上轉發」對方程式碼。

---

## 操作步驟（記錄供 audit）

```bash
# 0. 先確認 origin remote
git remote -v
# origin   https://github.com/zzw09773/ANILA.git

# 1. 把所有 remote branches 拉成 local（filter-repo 預設只動 local）
for r in anilaUI claude/developer-guide-docs-Ih9mU \
         claude/refactor-anila-ui-encryption-7qrFy \
         claude/restore-ui-from-template-VWf99 \
         codex/review main; do
  git branch --no-track "$r" "origin/$r"
done

# 2. 安全 backup tag
git tag pre-onyx-filter-repo-2026-04-27

# 3. 安裝 git-filter-repo（沒裝過的話）
python3 -m venv /tmp/filter-repo-venv
/tmp/filter-repo-venv/bin/pip install git-filter-repo

# 4. 從全 history 清掉 onyx/
/tmp/filter-repo-venv/bin/git-filter-repo --invert-paths --path onyx/ --force

# 5. filter-repo 會主動把 origin remote 拿掉（safety），補回去
git remote add origin https://github.com/zzw09773/ANILA.git

# 6. 確認沒有殘留
git log --all --name-only --pretty=format: | grep -c '^onyx/'
# 預期：0

# 7. 確認 .git 縮小
du -sh .git/
# 預期：~5–7 MB

# 8. force-push 所有 branches（高風險，需協調 collaborator）
# git push --force-with-lease origin --all
# git push --force-with-lease origin --tags
```

---

## ⚠️ 對 collaborator 的影響

**所有已經 clone 此 repo 的人**，在我們 force-push 後第一次拉新版時必須做：

```bash
# 替換掉本地任一 branch 的歷史
git fetch origin
git checkout <branch-name>
git reset --hard origin/<branch-name>
```

**有 unpushed work 的人**：請先 `git format-patch` 或 `git stash`，rewrite history 後再套用。

**有開 PR 的人**：PR 會因 base SHA 改變而標 `out of date`，需 rebase to new origin/<base>。

---

## 還原 plan（萬一）

如果 14 天內發現需要 Onyx 程式碼回來：

1. 不需要做 hard rollback — 從 `pre-onyx-filter-repo-2026-04-27` tag 撿一份 `onyx/` 出來：
   ```bash
   git checkout pre-onyx-filter-repo-2026-04-27 -- onyx/
   ```
2. 或直接 `git clone https://github.com/onyx-dot-app/onyx.git` 取最新 upstream

`pre-onyx-filter-repo-2026-04-27` tag 在 `pre push` 前**只在本地**，所以 force-push 不會把它推上去。如果想保險也推上 backup（建議），執行：
```bash
git push origin pre-onyx-filter-repo-2026-04-27
```

---

## 後續 follow-up

- [ ] **Force-push** to `github.com/zzw09773/ANILA.git` — 待 operator 在通知過 collaborator 後執行
- [ ] **Delete backup tag** ~14 天後，確認沒有 rollback 需求：`git tag -d pre-onyx-filter-repo-2026-04-27`
- [ ] **Notify agent team**：他們的 Onyx 程式碼專屬 repo URL 應記錄在 [`docs/archive/onyx-application-plan.md`](../archive/onyx-application-plan.md) 的 `## Repo location` section（如果還沒，補上）
