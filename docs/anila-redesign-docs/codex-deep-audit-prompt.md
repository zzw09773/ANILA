# Codex Deep Audit Prompt（Slice 0 交付物）

> 用途：每個 Slice 完成後，將本 prompt 連同該 Slice 的 diff 範圍交給 Codex 進行深度稽核。
> 憲法：`docs/anila-redesign-docs/00-product-constitution.md` 是唯一准入依據。

```text
You are auditing a slice of the ANILA redesign (branch: anila-redesign).
The sole constitution is docs/anila-redesign-docs/00-product-constitution.md.
The migration guardrails are docs/anila-redesign-docs/10-migration-and-development-guardrails.md.

Audit the given diff range against ALL of the following, in order:

1. Constitution admission contract (doc 00 §5): for every new feature/endpoint/table,
   identify which task, which entry, which source type, which capability dispatch,
   how it traces (trace_id), how it audits, whether it changes classification.
   Flag anything that introduces a new top-level product narrative without an ADR.

2. Frozen list (doc 00 §6): flag any change that ships a frozen item
   (unregistered agents, non-traced formal agents, GUI services outside the
   Service Registry, model API keys exposed to agents/frontend, OpenWebUI as
   runtime dependency, artifacts not bound to a Task).

3. Security invariants (never weaken): card SSO / RS256 JWT + JWKS / revocation /
   CSRF / RLS (csp_app runtime role, no superuser) / SSRF guard / one-way
   classification latch. Any diff touching these paths must be justified line-by-line.

4. Code-level guardrails (doc 10 §12): import rules (shell must not import model
   clients; runtime must not import UI; agents must not import CSP DB models;
   studio must not read CSP DB directly), runtime rules (all formal model/agent
   calls via CSP proxy; artifacts bound to task/source_snapshot; tasks carry trace_id),
   classification rules (no direct level lowering; no high-to-low copy; no low-ceiling
   capability processing high-classification tasks).

5. Schema changes: every schema change must have an Alembic migration with
   working downgrade; classified boolean -> five-level backfill must be floor-based
   and never destructive; alembic history must remain linear from the card-branch chain.

6. Tests: the slice's Done criteria (doc 10 §§2-11) must be covered by tests that
   actually run in CI/local (list the exact test files and confirm they exercise the
   new behavior, not just imports). Distinguish regressions from pre-existing failures.

7. Language policy (doc 11): all new user-facing frontend strings are Traditional
   Chinese (Taiwan usage); flag simplified characters or mainland terms.

Output: findings ranked by severity (BLOCKER / MAJOR / MINOR / NIT), each with
file:line, the violated rule, and a concrete fix. End with a verdict:
APPROVE / APPROVE-WITH-FIXES / REJECT.
```
