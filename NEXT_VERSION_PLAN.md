# Roamable — Next-Version Plan (mapped to the real codebase)

This maps Codex's proposed architecture onto what actually exists in the repo,
marks what's already built, records what I changed on this branch, and lays out
the remaining work with the real integration risks called out.

---

## TL;DR

Codex's **Phase 1 is essentially already built.** Auth, JWT verification,
per-user tables, per-user Strava linking, and "my vs everyone" coverage all
exist. The things that actually needed doing were (a) a login bug and (b) a free
alternative to Strava — both handled on this branch. What's genuinely *left* is
Codex's **Phase 2** (marker `difficulty + tags` cleanup, coverage-toggle polish,
onboarding polish), plus deciding what to do about Strava's new paywall.

---

## Status vs Codex's plan

### Core setup
- **Supabase Auth** — DONE. `backend/auth.py` validates the Supabase JWT against
  `/auth/v1/user`. Frontend uses `@supabase/supabase-js` with a login overlay.
- **Render Python backend** — DONE (unchanged home for the app + Postgres).
- **Postgres** — stays on Render (NOT Supabase's DB). Supabase is auth-only.
  This is a deliberate divergence from Codex's "Supabase Postgres" line — see
  "Open decisions" below.

### Backend auth pattern
- DONE. Frontend sends `Authorization: Bearer <supabase_jwt>` via the `apiFetch`
  wrapper; every `/api` route depends on `get_current_user`; all reads/writes are
  scoped by the authenticated `user_id`.

### Database shape (Codex's target vs actual)
| Codex table | Status in code |
|---|---|
| `profiles (id, display_name, created_at)` | DONE (`profiles`, created in `migrate_db`) |
| `tracks (id, user_id, path, source, recorded_at)` | DONE — `tracks` has `user_id`, `gpx_source`, `path` |
| `markers (id, user_id, location, difficulty, note, photo_url, …)` | PARTIAL — has `user_id`, `severity` (= difficulty), `note`, `photo_url`; still also has legacy `type` + `subtypes` JSONB |
| `marker_tags (marker_id, tag_key, tag_group)` | NOT DONE — tags currently live in `markers.subtypes` JSONB instead |
| `strava_accounts (user_id, athlete_id, tokens, expires_at)` | DONE |
| `strava_activities (user_id, activity_id, name, sport_type, status, start_date)` | DONE |
| `strava_oauth_states (state, user_id, expires_at)` | DONE (CSRF state for OAuth) |

### Coverage model
- DONE. `GET /api/tracks?scope=me|all|both` + `is_mine` flag; `GET /api/tracks/stats`
  returns `km_me` and `km_all`. Frontend has a `coverageMode` (me/all/both) menu
  toggle, renders "both" with mine stronger + community lighter, and shows the
  right km total.

### Strava flow
- DONE structurally (per-user link, OAuth state, review queue, import/dismiss).
- BLOCKED externally: Strava now gates API access behind a paid subscription
  (see "Strava" below). Not a code problem.

### Login flow / menu
- DONE: welcome/login overlay; menu has a user badge ("My profile" equivalent),
  dynamic Strava row (Link / connected), and Log out.

---

## What I changed on this branch

1. **Fixed the magic-link login bug.** Root cause: the email returned the
   *implicit* token shape (`#access_token=…&refresh_token=…`) which the client
   wasn't consuming (flow-type mismatch / detectSessionInUrl not catching it, made
   worse by the stale service worker). `initAuth` now captures the URL auth params
   *before* the client initialises and handles all three shapes explicitly:
   implicit hash → `setSession`, `token_hash` → `verifyOtp`, PKCE `?code=` →
   `detectSessionInUrl`. (`frontend/index.html`)

2. **Bumped the service-worker cache version** (`sw.js` v1 → v2) so deploys
   actually reach installed PWAs instead of serving a stale shell.

3. **SPA fallback route** in `backend/main.py` so an auth redirect to any path
   serves the app instead of a 404.

4. **`AUTH_DISABLED` bypass** (env flag) — backend returns a shared "guest" user
   and the frontend skips login, for emergencies/single-user use. Set
   `AUTH_DISABLED=true` on Render to open the app with no login; remove it to
   restore normal auth. `isSignedIn()` returns true in this mode so every feature
   works.

5. **In-app GPS walk recording** — the free Strava alternative. ☰ menu →
   "Record a walk" tracks live via `watchPosition`, draws the path, shows
   distance/time, and on Finish saves a track (`gpx_source: "recorded"`) straight
   to coverage. Holds a **screen Wake Lock** so the GPS keeps running while
   recording (foreground only — background tracking would need a native wrapper).
   No Strava/Garmin/subscription required. (`frontend/index.html`)

6. **Exploration vs Accessible tracks + Routes button.** Every track now has a
   `track_type` (`exploration` | `accessible`). On import/record you're asked
   which it is:
   - *Exploration* → deduped against existing coverage (only new ground stored).
   - *Accessible* → the **full** path is stored (so it can be followed), and its
     ground still counts toward explored coverage (coverage unions all tracks).
   A new green **Routes button** on the home screen toggles an "accessible routes"
   layer so you can see routes you already know work while out walking.
   Backend: `track_type` column + `GET /api/tracks?track_type=accessible`.
   (`backend/routers/tracks.py`, `backend/main.py`, `frontend/index.html`)

7. **DB made Supabase-pooler-safe** (`statement_cache_size=0`) ahead of the
   Postgres migration.

---

## Strava — the paywall, and the free path

Strava moved API portal access behind a paid subscription. Options:

- **Subscribe → make app → cancel:** unreliable. The subscription gates the
  developer portal; tokens may keep refreshing for a while, but Strava can disable
  non-subscriber API apps and you'd lose portal access to manage/repair it. Don't
  build on this.
- **In-app recording (SHIPPED this branch):** free forever, no third party.
- **GPX import (already existed):** export free from Garmin Connect / Strava,
  upload. Manual but free.
- **Garmin Activity API:** free (no subscription) but approval-gated — a possible
  future auto-sync path that avoids Strava entirely.

Recommendation: lead with in-app recording + GPX import; treat Strava sync as
"switch on later if you ever subscribe" (the code is already there and dormant).

---

## Remaining Phase 2 work (concrete)

### 1. Marker model → `difficulty` + reason `tags` (Codex's tagging model)
Map icons are **already** difficulty-driven, so the visual half is done. What's
left is the *authoring* model + picker.

- **Data:** keep it simple — store reason tags in the existing `markers.subtypes`
  JSONB as `[{group, key, label}]` and treat `severity` as the required
  `difficulty`. A separate `marker_tags` table (Codex's suggestion) is optional
  and adds migration/read churn for little near-term benefit; only worth it if you
  later need to query/aggregate by tag.
- **Frontend picker:** replace the category-first flow with:
  1. choose **difficulty** (Easy / Tricky / Hard) — buttons already exist,
  2. tap any **reason tags** grouped by family,
  3. optional note/photo → save.
- **Tag families** (from Codex): `surface` (mud, loose_stones, roots,
  uneven_ground), `width` (narrow, squeeze, overgrown), `gradient` (steep_up,
  steep_down, slippery), `barrier` (gate, stile, kissing_gate), `shortcut`
  (hedge_gap, field_gap).

⚠️ **Integration risk — passages/shortcuts:** the passage auto-bypass routing
(`backend/routers/routes.py` + `[[passage-gap-bypass]]`) keys off
`marker.type === 'passage'`. If "shortcut" becomes a *tag* instead of a *type*,
that routing breaks. Options: (a) keep `passage` as a first-class type even in the
tag world, or (b) migrate routing to detect the `shortcut` tag family. Needs a
deliberate decision, not a blind refactor.

### 2. Coverage toggle polish
Backend + basic UI done. Nice-to-haves: a clearer 3-way control (Mine / Everyone /
Both) instead of a cycling row, and a `contributors_count` stat
(`SELECT COUNT(DISTINCT user_id) FROM tracks`).

### 3. Onboarding polish
Login overlay exists. Codex wants a short value line ("track accessible routes
with friends"). Low effort; can tweak copy any time.

---

## Open decisions (need your input)

1. **Multi-user or just you?** Without Strava, per-user accounts mainly buy you
   "my vs everyone" coverage and a shared community map. If it's just you for now,
   running with `AUTH_DISABLED=true` (single shared guest) is simpler and avoids
   the whole magic-link flow. If you want friends contributing, keep real logins
   (now fixed).
2. **Supabase Postgres vs Render Postgres — MIGRATE to Supabase.** (Corrected:
   I initially under-weighted this.) Render's free Postgres is deleted after 30
   days, so staying there isn't viable for free. Supabase Postgres stays free and
   long-lived. Good news: it's a low-code move because the app talks to Postgres
   via `DATABASE_URL` and auto-creates all tables on boot. Steps:
     1. In Supabase → Database → Extensions, enable **PostGIS**.
     2. Copy the Postgres connection string (use the **Session pooler** or the
        direct connection; the code now also works with the transaction pooler
        since we set `statement_cache_size=0`).
     3. Set `DATABASE_URL` on Render to that string and redeploy — `migrate_db`
        creates every table/column on first boot.
     4. (Optional) migrate existing rows from the Render DB with `pg_dump`/`pg_restore`;
        if it's still test data, just start fresh.
   Note we're still using Supabase for *identity* the same way; this just moves the
   data store. No need to rewrite queries as RLS — the backend still enforces
   per-user access via the verified JWT.
3. **Passage/shortcut representation** before the marker-tag refactor (see risk
   above).

---

## Suggested next build order (from here)
1. Confirm decisions above (esp. multi-user vs single-user, passage handling).
2. Marker `difficulty + tags` picker refactor (frontend-led; data stays JSONB).
3. Coverage 3-way toggle + `contributors_count`.
4. Onboarding copy + small login polish.
