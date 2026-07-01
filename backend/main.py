import os
import asyncio
import asyncpg
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from routers.markers import router as markers_router
from routers.routes import router as routes_router
from routers.tracks import router as tracks_router

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://access:access@localhost:5432/accessmap")

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / "frontend"
SCHEMA_FILE = BASE_DIR / "db" / "init.sql"


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
        # v2: subtype column for two-step marker flow
        await conn.execute(
            "ALTER TABLE markers ADD COLUMN IF NOT EXISTS subtype TEXT;"
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
            "CREATE INDEX IF NOT EXISTS idx_tracks_path ON tracks USING GIST (path);"
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
            "CREATE INDEX IF NOT EXISTS idx_comments_marker ON comments (marker_id);"
        )
        # v5: 'field' marker type — ALTER TYPE cannot run inside a transaction
        # asyncpg executes each statement in its own implicit transaction, so this is safe
        await conn.execute(
            "ALTER TYPE marker_type ADD VALUE IF NOT EXISTS 'field';"
        )


async def create_pool_with_retry(url: str, retries: int = 10, delay: float = 3.0):
    """Retry DB connection — handles Render free-tier cold-start where DB wakes up after the app."""
    for attempt in range(1, retries + 1):
        try:
            return await asyncpg.create_pool(url, min_size=2, max_size=10)
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


app = FastAPI(title="AccessMap API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(markers_router, prefix="/api")
app.include_router(routes_router, prefix="/api")
app.include_router(tracks_router, prefix="/api")

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


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
