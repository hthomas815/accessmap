# AccessMap Refactor Plan

Branch off `main` before starting: `git checkout -b refactor/cleanup`

---

## Phase 1 — Quick wins (no breaking changes)

### 1.1 Fix import ordering in `markers.py`
`from db import get_db` is imported mid-file (line 20, after two class definitions). Move it to the top with the other imports. Zero behaviour change, just tidiness.

### 1.2 Clean up `requirements.txt`
Remove four unused packages that are left over from earlier iterations:
- `psycopg2-binary` — not used (asyncpg handles everything)
- `sqlalchemy[asyncio]` — not used
- `geoalchemy2` — not used
- `alembic` — not used (migrations are handled inline in `migrate_db()`)

### 1.3 Sync `db/init.sql` with actual schema
The init file is stale — it doesn't reflect the columns and tables added by migrations. Update it to be the canonical "fresh install" schema, including:
- `subtype TEXT` column on `markers`
- `field` value in the `marker_type` enum
- `still_valid BOOLEAN` column on `confirmations` (currently only in migration, not init)

This doesn't change runtime behaviour but means a fresh deploy would work correctly from init alone.

### 1.4 Fix the double semicolon in `index.html`
Line ~2469: `map.on("moveend", () => { ... });;` — extra semicolon, harmless but sloppy.

---

## Phase 2 — Image storage (Cloudinary)

**Why Cloudinary:** Free tier gives 25 GB storage + 25 GB bandwidth/month. No credit card required. Returns a permanent URL — no DB bloat, no base64.

### What changes

**Backend (`markers.py`)**
- Add `cloudinary` to `requirements.txt`
- On `POST /api/markers`, instead of base64-encoding the photo, upload it to Cloudinary and store the returned URL in `photo_url`
- Needs two new env vars: `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`
- Set these in Render dashboard under Environment Variables

**No frontend changes needed** — the frontend already uses `photo_url` as a plain `<img src>`, so a real URL works identically to a base64 string.

**No DB changes needed** — `photo_url TEXT` column already exists.

### Migration path for existing base64 photos
Existing markers already in the DB have base64 `photo_url` values. Options:
- Leave them — browsers handle both fine
- Write a one-off migration script to re-upload existing base64 images to Cloudinary (do this after Cloudinary is wired up)

---

## Phase 3 — Frontend refactor (split the monolith)

`frontend/index.html` is 2,565 lines of mixed HTML, CSS, and JS. Split into three files:

```
frontend/
  index.html      (~120 lines — just structure + script/link tags)
  app.css         (~800 lines — all styles)
  app.js          (~1,600 lines — all JavaScript)
```

FastAPI already serves the whole `frontend/` directory as `/static`, so adding `app.css` and `app.js` requires no backend changes — just update the `<link>` and `<script>` tags in `index.html`.

The service worker's `APP_SHELL` list in `sw.js` will need `app.css` and `app.js` added (and the inline versions removed).

### Within `app.js` — consider further splitting (Phase 3b, later)
Once the file is extracted, natural module boundaries exist:
- `icons.js` — ICONS, SUBTYPE_ICONS constants
- `map.js` — Leaflet setup, marker loading, clustering
- `ui.js` — sheet/panel open/close, toast, form state
- `api.js` — all `fetch()` calls to `/api/...`
- `routes.js` — route mode logic
- `tracks.js` — GPX import, coverage layer, grid

This would require either a bundler (Vite/esbuild) or ES module `<script type="module">` imports. No bundler is simpler for now given the Render free tier setup.

---

## Phase 4 — Authentication (deferred)

No auth is the biggest real-world gap. Options in rough order of effort:

1. **Simple shared PIN / passphrase** — gate the `POST`, `PATCH`, `DELETE` endpoints behind a header or form field. Dead simple, appropriate if this is a small trusted group.
2. **Supabase Auth** — free, handles Google/magic-link login, JWT-based. Would pair well with Supabase Storage if that's chosen for images.
3. **Clerk or Auth0 free tier** — more polished but more setup.

Recommendation: start with option 1 (a single `X-API-Key` header checked on write endpoints) and upgrade later.

---

## Phase 5 — CORS hardening

Change `allow_origins=["*"]` to the actual deployed domain(s):
```python
allow_origins=[
    "https://your-app.onrender.com",
    "http://localhost:8000",  # dev only
]
```

---

## Suggested order of execution

| # | Task | Risk | Effort |
|---|---|---|---|
| 1 | Fix import order, dead deps, init.sql sync | None | 30 min |
| 2 | Cloudinary image upload | Low | 1–2 hrs |
| 3 | Split frontend into HTML/CSS/JS | Low–Medium | 2–3 hrs |
| 4 | Auth (PIN-based) | Medium | 2 hrs |
| 5 | CORS hardening | None | 10 min |
| 6 | ES module split (Phase 3b) | Medium | 3–4 hrs |

---

## Pre-refactor checklist
- [ ] `git checkout -b refactor/cleanup`
- [ ] Cloudinary account created and API keys noted
- [ ] Render env vars added (CLOUDINARY_*)
- [ ] Confirm existing tests pass (there are none — add at least a smoke test before Phase 4)
