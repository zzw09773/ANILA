# Phase 4 — Pre-Rewrite Backup

This directory snapshots the Phase 2 reranker + OCR implementations before
Phase 4 replaced them with a single vLLM-based reranker backend and a single
vision-API-based OCR backend.

## Contents

| Archived file | Replaced by |
|---|---|
| `src/providers/reranker.py` | Same path — rewritten with `VllmScoreRerankerProvider` only |
| `src/ingestion/ocr.py` | Same path — rewritten with `VisionApiOcrBackend` only |
| `tests/test_reranker.py` | Same path — rewritten against vLLM `/v1/score` mock |
| `tests/test_ocr_fallback.py` | Same path — heuristic tests preserved, backend tests replaced |

## What changed and why

The Phase 2 design supported three reranker backends (`jina`, `local`,
plus an unbuilt slot) and three OCR backends (`easyocr`, `tesseract`,
plus an unbuilt slot) so the codebase could run in either cloud or
purely-local-CPU environments.

Phase 4 narrowed that down because the deployment target turned out to
be a closed internal network with 4× H100 + Triton/vLLM/TensorRT-LLM
already serving LLM, embedding, and vision. In that environment:

- **Jina cloud** is unreachable (no outbound)
- **Local PyTorch reranker** is wasteful (model server has GPUs, app machines don't)
- **EasyOCR / Tesseract on the app machine** is slow vs offloading
  to the existing vision LLM (`meta/llama-4-maverick`)

So the surface was deliberately collapsed to a single path each:

- Reranker → vLLM hosting `mixedbread-ai/mxbai-rerank-large-v1`
  via OpenAI-compatible `/v1/score`
- OCR → existing `VISION_URL` (llama-4-maverick) with a prompt-based
  page-by-page request

## When to consult this archive

- You want to re-add a CPU/local fallback path for dev environments
- You want to support an off-network deployment that needs the Jina cloud or pure-CPU OCR
- You need to remember how the old `JinaRerankerProvider` request body
  was shaped, or how `EasyOcrBackend.extract` rasterised PDF pages

## When to delete this archive

After Phase 4 has been validated against a real H100 server and a few
real Traditional Chinese PDFs end-to-end.

---

## Archive metadata

| Field | Value |
|---|---|
| Archived on | 2026-04-07 |
| Phase | 4 (vLLM reranker + Vision-API OCR unification) |
| Current template location | [`../../`](../../) (ANILA monorepo after 2026-04-24 migration) |
| Deployment spec for the new design | [`../../docs/SERVER_DEPLOYMENT_PHASE4.md`](../../docs/SERVER_DEPLOYMENT_PHASE4.md) |

**Last updated**: 2026-04-24
