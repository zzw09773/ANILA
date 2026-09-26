# share/

> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】,保留供追溯,不代表現況。現行狀態與執行順序見 `PLAN.md`。

Runtime data for the ANILA nginx container. `/static/*` is served publicly;
`/uploads/` is not public (nginx returns 404). Both subdirectories are git-ignored —
the contents are workflow-specific assets and user uploads, not source code.

## Layout

```
share/
├── static/    # served at /static/*  (read-only)
│   ├── fina./           ← finance / data-quality form HTML templates that
│   │                      n8n workflows fetch by URL
│   ├── icons/           ← service icons (mlsteam.png / gitlab.png / ...); brought in
│   │                      from the prod source by the "Migrating" step below, empty initially
│   └── ...              ← any other static assets the workflows reference
├── uploads/   # bind-mounted rw; nginx does not serve this tree
│   ├── ingestion/      ← raw upload blobs + parse artifacts from CSP; mounted as the
│   │                     ingestion-worker UPLOAD_DIR (/var/anila/ingestion-uploads);
│   │                     captioned images land in anila-images/<doc_id>/;
│   │                     NOT served by nginx (404)
│   └── mock_11406/     ← finance sample xlsx test data (NOT served)
├── pki/                # certificate / key material (runtime; contents git-ignored)
└── codeserver-sandbox/ # code-server sandbox workspace (git-ignored; only .gitkeep kept)
```

## Ownership

- `static/` is `:ro` mounted into nginx — files are read-only at runtime.
  Drop new templates / icons in via `cp` from the host shell.
- `uploads/` is `:rw` mounted — services that are actually running can write here. n8n is **not** in the default ship (`COMPOSE_PROFILES=ops`). Don't
  put anything you can't afford to lose; back up out-of-band.

nginx (`infra/nginx/anila.conf`) serves `/static/` with `try_files =404`
and `expires 1y, immutable`. `/uploads/` returns 404.

## Migrating from My-OpenAI-Frontend

The prod stack at `/home/aia/c1147259/project/My-OpenAI-Frontend/share/`
served the same role. To bring assets across:

```bash
mv /home/aia/c1147259/project/My-OpenAI-Frontend/share/static/*  ./static/
mv /home/aia/c1147259/project/My-OpenAI-Frontend/share/uploads/* ./uploads/
```

Existing n8n workflows that referenced
`https://172.16.120.35/static/fina./template.html` keep working as-is —
the URL path layout is identical.
