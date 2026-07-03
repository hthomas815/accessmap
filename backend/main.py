import os
import asyncio
import asyncpg
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from routers.markers import router as markers_router
from routers.routes import router as routes_router
from routers.tracks import router as tracks_router
from routers.strava import router as strava_router
from routers.areas import router as areas_router

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://access:access@localhost:5432/accessmap")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / "frontend"
SCHEMA_FILE = BASE_DIR / "db" / "init.sql"
if not SCHEMA_FILE.exists():
    SCHEMA_FILE = BASE_DIR.parent / "db" / "init.sql"


async def init_db(pool: asyncpg.Pool) -> None:
    """Run full schema SQL on first boot if markers table doesn't exist."""
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'markers')"
        )
        if not exists and SCHEMA_FILE.exists():
            sql = SCHEMA_FILE.read_text()
            await conn.execute(sql)


async def migrate_db(pool: asyncpg.Pool) -> None:
    """Idempotent migrations — safe to run on every boot."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id           TEXT PRIMARY KEY,
                email        TEXT,
                display_name TEXT,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        # v2: subtype column for two-step marker flow
        await conn.execute(
            "ALTER TABLE markers ADD COLUMN IF NOT EXISTS subtype TEXT;"
        )
        await conn.execute(
            "ALTER TABLE markers ADD COLUMN IF NOT EXISTS user_id TEXT;"
        )
        # v3: tracks table for GPX coverage layer
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id          SERIAL PRIMARY KEY,
                name        TEXT,
                path        GEOMETRY(LineString, 4326),
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                gpx_source  TEXT
            );
        """)
        await conn.execute(
            "ALTER TABLE tracks ADD COLUMN IF NOT EXISTS user_id TEXT;"
        )
        # v10: track_type — 'exploration' (coverage only) or 'accessible' (a known-good route)
        await conn.execute(
            "ALTER TABLE tracks ADD COLUMN IF NOT EXISTS track_type TEXT NOT NULL DEFAULT 'exploration';"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_path ON tracks USING GIST (path);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_type ON tracks (track_type);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_user_id ON tracks (user_id);"
        )
        # v4: comments table for marker updates
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id          SERIAL PRIMARY KEY,
                marker_id   INTEGER NOT NULL REFERENCES markers(id) ON DELETE CASCADE,
                body        TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        await conn.execute(
            "ALTER TABLE comments ADD COLUMN IF NOT EXISTS user_id TEXT;"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comments_marker ON comments (marker_id);"
        )
        await conn.execute(
            "ALTER TABLE confirmations ADD COLUMN IF NOT EXISTS user_id TEXT;"
        )
        # v5: 'field' marker type — ALTER TYPE cannot run inside a transaction
        # asyncpg executes each statement in its own implicit transaction, so this is safe
        await conn.execute(
            "ALTER TYPE marker_type ADD VALUE IF NOT EXISTS 'field';"
        )
        # v6: passage marker type for off-map walkable gaps
        await conn.execute(
            "ALTER TYPE marker_type ADD VALUE IF NOT EXISTS 'passage';"
        )
        # v7: multiple subtype tags per marker (JSONB array of {type, key, label})
        await conn.execute(
            "ALTER TABLE markers ADD COLUMN IF NOT EXISTS subtypes JSONB NOT NULL DEFAULT '[]'::jsonb;"
        )
        # v8: Strava OAuth tokens (single-user MVP: one row)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS strava_accounts (
                athlete_id    BIGINT PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    BIGINT NOT NULL,
                firstname     TEXT,
                lastname      TEXT,
                connected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        await conn.execute(
            "ALTER TABLE strava_accounts ADD COLUMN IF NOT EXISTS user_id TEXT;"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_strava_accounts_user_id ON strava_accounts (user_id) WHERE user_id IS NOT NULL;"
        )
        # v9: remember which Strava activities were already imported / dismissed
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS strava_activities (
                activity_id   BIGINT PRIMARY KEY,
                athlete_id    BIGINT,
                name          TEXT,
                sport_type    TEXT,
                distance_m    DOUBLE PRECISION,
                start_date    TIMESTAMPTZ,
                status        TEXT NOT NULL DEFAULT 'pending',
                seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        await conn.execute(
            "ALTER TABLE strava_activities ADD COLUMN IF NOT EXISTS user_id TEXT;"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_strava_activities_user_status ON strava_activities (user_id, status);"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS strava_oauth_states (
                state       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL
            );
        """)
        # v11: manually-claimed explored areas (open spaces/fields the user has roamed).
        # Stored as MultiPolygon so both single OSM ways and multipolygon relations fit.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS claimed_areas (
                id          SERIAL PRIMARY KEY,
                user_id     TEXT,
                osm_type    TEXT,
                osm_id      BIGINT,
                name        TEXT,
                kind        TEXT,
                area        GEOMETRY(MultiPolygon, 4326) NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claimed_areas_geom ON claimed_areas USING GIST (area);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claimed_areas_user ON claimed_areas (user_id);"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_claimed_areas_user_osm "
            "ON claimed_areas (user_id, osm_type, osm_id) WHERE osm_id IS NOT NULL;"
        )


async def create_pool_with_retry(url: str, retries: int = 10, delay: float = 3.0):
    """Retry DB connection — handles Render free-tier cold-start where DB wakes up after the app."""
    for attempt in range(1, retries + 1):
        try:
            # statement_cache_size=0 keeps us compatible with Supabase's
            # transaction pooler (pgbouncer), which rejects prepared statements.
            return await asyncpg.create_pool(url, min_size=2, max_size=10, statement_cache_size=0)
        except Exception as exc:
            if attempt == retries:
                raise
            print(f"DB connect attempt {attempt}/{retries} failed ({exc}), retrying in {delay}s…")
            await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await create_pool_with_retry(DATABASE_URL)
    await init_db(pool)
    await migrate_db(pool)
    app.state.pool = pool
    yield
    await pool.close()


app = FastAPI(title="Roamable API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(markers_router, prefix="/api")
app.include_router(routes_router, prefix="/api")
app.include_router(tracks_router, prefix="/api")
app.include_router(strava_router, prefix="/api")
app.include_router(areas_router, prefix="/api")

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/app-config")
async def app_config():
    return {
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
        "auth_disabled": os.getenv("AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"),
    }


@app.get("/sw.js")
async def serve_sw():
    f = FRONTEND_DIR / "sw.js"
    return FileResponse(str(f), media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})


@app.get("/manifest.json")
async def serve_manifest():
    f = FRONTEND_DIR / "manifest.json"
    return FileResponse(str(f), media_type="application/manifest+json")


@app.get("/")
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"detail": "Frontend not found"}


# SPA fallback: serve index.html for any non-API path so auth redirects
# (e.g. magic-link landings on /auth/... or unknown routes) load the app
# instead of returning a 404. Registered last so it only catches leftovers.
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("static/"):
        raise HTTPException(status_code=404, detail="Not found")
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail="Not found")
