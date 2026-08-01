# share/

Runtime data for the ANILA nginx container. `/static/*` is served publicly;
`/uploads/` is allowlisted so only `/uploads/flux/*` is reachable (everything
else under `/uploads/` returns 404). Both subdirectories are git-ignored —
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
├── uploads/   # bind-mounted rw; nginx only serves /uploads/flux/*
│   ├── ingestion/      ← raw upload blobs + parse artifacts from CSP; mounted as the
│   │                     ingestion-worker UPLOAD_DIR (/var/anila/ingestion-uploads);
│   │                     captioned images land in anila-images/<doc_id>/;
│   │                     NOT served by nginx (404)
│   ├── flux/           ← FLUX image-generation output cache (PUBLIC_URL_PREFIX)
│   └── mock_11406/     ← finance sample xlsx test data (NOT served)
├── pki/                # certificate / key material (runtime; contents git-ignored)
└── codeserver-sandbox/ # code-server sandbox workspace (git-ignored; only .gitkeep kept)
```

## Ownership

- `static/` is `:ro` mounted into nginx — files are read-only at runtime.
  Drop new templates / icons in via `cp` from the host shell.
- `uploads/` is `:rw` mounted — n8n / other services can write here. Don't
  put anything you can't afford to lose; back up out-of-band.

nginx (`infra/nginx/anila.conf`) serves these: `/static/` uses `try_files =404`
with `expires 1y, immutable`; `/uploads/flux/` uses `expires 1h`; bare
`/uploads/` and other subtrees return 404 (no SPA fallback).

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
