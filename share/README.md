# share/

Runtime data served by the ANILA nginx container at `/static/*` and
`/uploads/*`. Both subdirectories are git-ignored — the contents are
workflow-specific assets and user uploads, not source code.

## Layout

```
share/
├── static/    # served at /static/*  (read-only)
│   ├── fina./           ← finance / data-quality form HTML templates that
│   │                      n8n workflows fetch by URL
│   ├── icons/           ← service icons (mlsteam.png / gitlab.png / ...)
│   └── ...              ← any other static assets the workflows reference
└── uploads/   # served at /uploads/* (read-write)
    └── document/        ← user-uploaded source documents (embeddings input, etc.)
```

## Ownership

- `static/` is `:ro` mounted into nginx — files are read-only at runtime.
  Drop new templates / icons in via `cp` from the host shell.
- `uploads/` is `:rw` mounted — n8n / other services can write here. Don't
  put anything you can't afford to lose; back up out-of-band.

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
