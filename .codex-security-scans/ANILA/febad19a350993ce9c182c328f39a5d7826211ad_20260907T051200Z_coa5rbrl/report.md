# Security Review: ANILA

## Scope

The scan was configured for the include paths and exclusions listed below.

- Scan mode: repository
- Target kind: git_worktree
- Target ID: target_sha256_e5dda1e1b0539b1f494ab95a4a354f3a16977a434b1b4ef0452f111c085bef14
- Revision: febad19a350993ce9c182c328f39a5d7826211ad
- Snapshot digest: codex-security-snapshot/v1:sha256:571a198feb03100ef2e40d9c1bacfa1d9c6b4a1fd2f60b21f2d383fe8e95b1f7
- Inventory strategy: repository
- Included paths: .
- Excluded paths: none
- Runtime or test status: not recorded

Limitations and exclusions:
- Excluded node_modules/\*\*: Third-party dependency trees are background, not product source.
- Excluded dist/\*\*: Generated build output is background unless a live flow reaches generated code.
- Excluded __pycache__/\*\*: Compiled cache is not source.

### Scan Summary

| Field | Value |
| --- | --- |
| Scan outcome | completed |
| Reportable findings | 0 |
| Severity mix | none |
| Confidence mix | none |
| Coverage | partial |
| Validation mode | not recorded |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

No explicit canonical threat-model summary was recorded.

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| CSP authentication and access control | not recorded | No issue found | Reviewed RS256/JWKS token verification, caller identity extraction, API-key validation, and service access-control algorithm. |
| CSP attachment upload and download | not recorded | No issue found | Reviewed upload extension/MIME/size controls, ownership checks, conversation binding, and download authorization. |
| Outbound URL and SSRF guard | not recorded | No issue found | Reviewed shared validate_outbound_url use and call sites for model, agent, ingestion, authentication, health, memory, and thinking-probe endpoints. |
| Studio and core subprocess boundaries | not recorded | No issue found | Reviewed diagram/mindmap renderer exec-style subprocesses and core shell tool sandboxing boundaries. |

## Open Questions And Follow Up

- Delegation was unavailable in this runtime; the whole-repository tracked-source review is partial and the remaining surfaces require follow-up.
  - Follow-up prompt: Review deferred unit remaining-tracked-source and close its stated proof gap. Paths: apps, infra, packages, services.
- Dependency CVE and secret-history scanning were outside this manual source-backed standard scan pass.
  - Follow-up prompt: Review deferred unit dependency-cve-and-secret-scans and close its stated proof gap.
