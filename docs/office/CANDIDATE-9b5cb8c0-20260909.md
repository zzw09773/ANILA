# ANILA candidate freeze — 2026-09-09

Commander: ANILA Grok指揮官 (`01a08452-003f-7b11-89ce-8f3561bb9b16`).
Owner authorization this turn: integrate the reviewed A01/F6 isolated patch, then open P2.6 / image / `.15`.

## Candidate

| Field | Value |
|---|---|
| Branch | `codex/fix-a01-f6-20260908` |
| Worktree | `/tmp/anila-a01-f6-20260908` |
| Candidate SHA | `9b5cb8c06d8d1dafefa54a3ddac4f3abf0034919` |
| Current candidate SHA | `8c394d91d301976773cdf12d1ca3af4b3be06cce` (adds CSP curl for F6 verify) |
| Base | `3a9caf33c34094f60002a7f21c9752f68a2fe805` (`v1.2.1` / `main`) |
| Commit | `fix(csp): fail-closed empty allowed_origins and propagate deploy verify failures` |
| Push / tag / main merge | not done |

Content hashes match the 2026-09-08 accepted isolated delivery (`final.patch` `217c48d1…`, tracked diff `bef8829a…`).

## Commander verification after freeze

- `pytest` on worktree `services/csp` (`REDIS_URL=redis://127.0.0.1:1`, skip alembic-only tests): **78 passed, 2 deselected**.
- F6 PATH-stub harness: **9 passed, 0 failed**.
- Qwen independent A01 fixtures were not re-run; product hashes were unchanged from the reviewed tree.

## P2.6 / image / `.15`

Evidence directory: `/home/c1147259/anila-private-audits/p26-candidate-9b5cb8c0-20260909/` (0700, not git).

Live stack remains `anila-restart-*` (healthy ~44h). `codeserver` still uses `anila-codeserver:local`. Candidate compose project `anila-release-candidate` isolates other built images, but **codeserver/codeserver-init still share that live tag** and must be overridden before any rebuild.

| Gate | Status |
|---|---|
| pip-audit CLI on six freezes | ran; failed on first-party pins; not treated as clean |
| OSV querybatch (108 third-party pins) | done: ecdsa 0.19.2 (shipped), pytest 8.4.2 (csp freeze) |
| npm audit (4 locks, full + production) | done; pptx-renderer production HIGH remains |
| Bandit 1.9.4 | done (raw); 1 HIGH SHA1, 86 MEDIUM, 11383 LOW |
| Semgrep 1.173.0 | `p/python` + JS/TS/Dockerfile done; dedicated nginx pack not run |
| Fresh image build | done: project `anila-release-candidate`, 13 tars, artifact scan clean, CHECKSUMS 13/13 |
| Trivy 0.74.0 | done on candidate built images + nginx/redis/pgvector/n8n/gitlab; counts in SCAN-STATUS.md |
| `.15` / P2.7 live DB | not executed (requires host 10.53.100.15); see DOT15-STATUS.md |

Q22 / A10 remain owner policy, not in this candidate commit.
Fable P3 follow-ups were not expanded.

Astra is contract/architecture/high-risk/release signing only; not used as daily dispatch.
Private scan evidence: `/home/c1147259/anila-private-audits/p26-candidate-9b5cb8c0-20260909/SCAN-STATUS.md`.
This is a candidate freeze plus a partial P2.6 pass, not a closed release gate.
GLM read-only dep triage (2026-09-09): three shipped findings all `not_block`. Report copied to the evidence dir after GLM could not write there. Commander verified SHA `3956595b…` and spot-checked import/JWT/sharp/echarts paths; accepted the verdicts for this freeze.
Image export completed without touching live `anila-restart`. CSP image curl gate closed on the candidate tag only. `.15` remains a remote intranet deploy, not this workstation.
