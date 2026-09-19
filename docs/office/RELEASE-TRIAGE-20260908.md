# ANILA release preparation — 2026-09-08

Batch: BATCH-20260908-RELEASE-TRIAGE. Preparation complete; release gates remain open.
Parent: ANILA_Astra (`01a07ff4-2acc-7ab2-8aec-2456ace0a6b3`).
Baseline: main / v1.2.1 peeled commit `3a9caf33c34094f60002a7f21c9752f68a2fe805`; local origin/main same. No remote refresh.
Cwd: `/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA`.

Scope: five-claim static triage, independent test design review, local scan/build feasibility and input inventory, manual preparation outline.
No product edit, dependency scan, build, deployment, commit/push/tag or .15 verification performed. Existing untracked `.claude/` and `.codex-security-scans/` excluded and preserved.
Parent is sole writer of this new project-local checkpoint. Private detailed evidence is not added to git.

## Session index and ownership

| Responsibility | Task/session | Model route | Outcome |
|---|---|---|---|
| Source inspection | ANILA_Grok / 01a07ff6-13a6-7330-83a7-8cb219064b1f | xai/grok-4.6, existing ultra | Complete, static only |
| Initial QA design | ANILA_Qwen / 01a07ffd-b33b-7f02-9793-e46cbfe651dc | litellm/qwen38-flash-next, existing xhigh | Complete; mistaken Router Q22 withdrawn and corrected |
| Primary cross-family review | ANILA_GLM / 01a07ffa-f32f-7502-8941-f48739375c10 | litellm/glm-5.3-flash, existing ultra | Five-item review plus bounded three-item retry recovered from session JSONL |
| Secondary review | ANILA_Luna / 01a08001-35a8-7143-8605-cfa7f4e17798 | gpt-5.6-luna, medium | approve_with_conditions; conditions incorporated; design review only |
| Manual preparation | agy / 537cccab-af5b-404f-bb79-f374fddc8e43 | gemini-3.8-flash-medium | Two CLI turns, corrected outline only |

Desktop session metadata reports provider `openai` even for routed names; this is harness metadata, not independent backend identity proof. Named route preserved or explicitly changed per user; global model default untouched.
Sol first attempt failed on usage; resumed turn ended interrupted with no final. User then explicitly replaced secondary review with Luna, renamed the same task, and global `/home/c1147259/.codex/AGENTS.md` was updated/read back: review/sampling/fallback/Qwen assertion review -> Luna medium; high-risk additional review -> Fable.
App wait/read omitted GLM and Luna message bodies; parent recovered exact final messages from known session JSONL and checked actual source. No empty response counted as pass.
Cursor Fable was not invoked: this batch produced no architectural/code changes and no unresolved cross-family verdict dispute requiring escalation. agy route verified via CLI model listing; resumed with explicit conversation ID.

## Durable evidence

Private directory (0700, files 0600): `/home/c1147259/anila-private-audits/release-triage-20260908-astra/`.

- `DECISION-PACK.md`: five findings, evidence, counterevidence, proposed scope and unresolved contracts.
- `QA-ACCEPTANCE-MATRIX.md`, `luna-secondary-review.md`: parent corrections and independent secondary review, all behavior cases NOT RUN.
- `scan-build-plan.md`: proposed bounded local isolation, input/provenance and coverage gates.
- `input-manifest.json`, `effective-images.json`, `triage.json`: six freeze/four npm lock hashes, metadata-only compose inventory and per-input verdicts.
- `manual-preparation-draft.md`: general-user vs operator outline, .15 observations still required. Not Stage 9 HTML delivery.
- Raw reviewer outputs and pre-Luna versions retained; `CHECKSUMS.sha256` covers 18 evidence files. Writes read back byte-identical to staging.

## Current conclusions and next action

- Proposed next repair scope: A01/F6. Artifact owner policy (input Q22, specifically OWNER-QUESTIONS Q22(a)) and A10 require contract disposition; A07 retains recorded internal-network trust policy, .15 assumptions unverified.
- No user risk acceptance inferred from this technical recommendation. Candidate SHA/tag depends on final repair disposition; no tag created.
- Local feasibility confirmed: Docker 28.2.2 reachable with approved escalation; ~319 GiB project disk, ~29 GiB available RAM, 24 logical CPUs at inspection. User reopened earlier local-scan restriction. Existing isolated pip-audit/Bandit/Semgrep files found; no version, health or vulnerability DB acceptance yet; Trivy not found on PATH.
- Local .env used only for compose metadata rendering: 14 default-profile service rows, 13 unique images, 8 unique built images. No expanded secret values retained. codeserver uses fixed `anila-codeserver:local`; project override alone does not isolate it. Candidate image override needed before build.
- Old wheelhouse final addendum pins a385bd7c; anila-core packaging changed by v1.2.1. Re-establish local wheel provenance and final installed-vs-manifest closure; equal versions are insufficient.
- Root CLAUDE.md absence explained by HANDOFF-2026-08-31.md as intentional removal, not a repair target.
- Next: owner contract disposition -> isolated authorized repair/test/review -> final candidate provenance -> full P2.6 source/dependency/fresh-image scans with Q54 repeat capacity -> final-tag bundle -> .15 acceptance/P2.7 live DB -> final user manual.

## A01/F6 execution authorized; direct peer handoff

User authorized A01/F6 implementation and changed coordination: parent dispatch/final receipt only; workers directly notify next peer, blockers to parent.
Worktree `/tmp/anila-a01-f6-20260908`, branch `codex/fix-a01-f6-20260908`, baseline 3a9caf33. Work order `/tmp/anila-a01-f6-handoff-20260908/WORK-ORDER.md` defines scopes/contracts/contact IDs and Grok -> Qwen -> GLM -> Luna/Fable -> Grok final chain. No commits/push/tag/main integration authorized; result is reviewed isolated patch. Parent does not relay ordinary handoffs.
