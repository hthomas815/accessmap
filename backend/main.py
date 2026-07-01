import os
import asyncpg
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from routers.markers import router as markers_router

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://access:access@localhost:5432/accessmap")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/app/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
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

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"detail": "Frontend not found"}
