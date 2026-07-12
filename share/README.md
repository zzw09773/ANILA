# share/

Runtime data served by the ANILA nginx container at `/static/*` and
`/uploads/*`. Both subdirectories are git-ignored — the contents are
workflow-specific assets and user uploads, not source code. `/static/*` is
public same-origin content; `/uploads/*` requires a CSP smart-card session.

## Layout

```
share/
├── static/    # served at /static/*  (read-only)
│   ├── fina./           ← finance / data-quality form HTML templates that
│   │                      n8n workflows fetch by URL
│   ├── icons/           ← service icons (mlsteam.png / gitlab.png / ...); brought in
│   │                      from the prod source by the "Migrating" step below, empty initially
│   └── ...              ← any other static assets the workflows reference
├── uploads/   # served at /uploads/* (read-write)
│   ├── ingestion/      ← raw upload blobs + parse artifacts from CSP; mounted as the
│   │                     ingestion-worker UPLOAD_DIR (/var/anila/ingestion-uploads);
│   │                     captioned images land in anila-images/<doc_id>/
│   ├── flux/           ← FLUX image-generation output cache
│   └── mock_11406/     ← finance sample xlsx test data
├── pki/                # certificate / key material (runtime; contents git-ignored)
└── codeserver-sandbox/ # opt-in code-server workspace (git-ignored; never the prod repo root)
```

## Ownership

- `static/` is `:ro` mounted into nginx — files are read-only at runtime.
  Drop new templates / icons in via `cp` from the host shell.
- `uploads/` is `:rw` mounted — n8n / other services can write here. Don't
  put anything you can't afford to lose; back up out-of-band.
- `codeserver-sandbox/` is the only host path mounted into the opt-in
  code-server container. Put a separate internal-GitLab clone here; do not
  copy the production `.env`, TLS/JWT keys, PKI material, or backup sets into
  it. Start/stop it with `deploy-prod.sh codeserver-up|codeserver-down`.

nginx (`infra/nginx/anila.conf`) serves these: `/static/` uses `try_files =404`
with `expires 1y, immutable`. `/uploads/ingestion/` is never served and returns
404; `/uploads/flux/` and the generic `/uploads/` fallback require a CSP
smart-card session and return `Cache-Control: private, no-store` plus
`Referrer-Policy: no-referrer`. Missing assets return 404 (no SPA fallback).

This browser-session gate is not a machine-ingress contract. Git CLI uses the
separate GitLab Shell SSH port with SSH keys; n8n webhooks, runners, API clients,
and other service callers must not receive an anonymous exception under
`/uploads/`. Use an internal volume/API today and a dedicated mTLS or signed
machine ingress when that contract is implemented.

## Migrating from My-OpenAI-Frontend

The prod stack at `$HOME/project/My-OpenAI-Frontend/share/`
served the same role. To bring assets across:

```bash
mv $HOME/project/My-OpenAI-Frontend/share/static/*  ./static/
mv $HOME/project/My-OpenAI-Frontend/share/uploads/* ./uploads/
```

Existing n8n workflows that referenced
`https://172.16.120.35/static/fina./template.html` keep working as-is —
the URL path layout is identical.
