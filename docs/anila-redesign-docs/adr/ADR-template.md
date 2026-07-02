# ADR-XXXX: <決策標題>

> Status: proposed | accepted | superseded by ADR-YYYY
> Date: YYYY-MM-DD
> Deciders: <拍板者>
> Related: <關聯文件 / ADR / Slice>

## 背景

<為什麼需要這個決策；現況與限制條件。>

## 決策

<一句話寫清楚決定了什麼。>

## 理由

<為什麼選這個方案而非替代方案。>

## 替代方案

- <方案 A>：<為何不採用>
- <方案 B>：<為何不採用>

## 影響

- <對架構 / 契約 / 遷移 / 安全不變量的影響>
- <需要跟進的工作項>

## 憲法檢核

- [ ] 不違反 `00-product-constitution.md` §5 功能准入合約
- [ ] 不落入 §6 凍結清單；若落入，已在本 ADR 明述例外理由
- [ ] 不弱化安全不變量（card SSO / JWT / CSRF / RLS / SSRF guard / 單向閂鎖）
