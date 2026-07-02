import os
import time
from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException
from asyncpg import Connection


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

_TOKEN_CACHE: dict[str, tuple[float, "AuthUser"]] = {}
_CACHE_TTL_S = 300


@dataclass
class AuthUser:
    id: str
    email: str | None = None
    display_name: str | None = None


def _configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


async def get_current_user(authorization: str | None = Header(None)) -> AuthUser:
    if not _configured():
        raise HTTPException(status_code=503, detail="Supabase auth is not configured on the server.")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    now = time.time()
    cached = _TOKEN_CACHE.get(token)
    if cached and cached[0] > now:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_ANON_KEY,
                },
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Supabase auth is unavailable: {exc}") from exc

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")

    data = resp.json()
    meta = data.get("user_metadata") or {}
    user = AuthUser(
        id=data["id"],
        email=data.get("email"),
        display_name=meta.get("full_name") or meta.get("name") or data.get("email"),
    )
    _TOKEN_CACHE[token] = (now + _CACHE_TTL_S, user)
    return user


async def ensure_profile(db: Connection, user: AuthUser) -> None:
    await db.execute(
        """
        INSERT INTO profiles (id, email, display_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (id) DO UPDATE
        SET email = EXCLUDED.email,
            display_name = COALESCE(EXCLUDED.display_name, profiles.display_name)
        """,
        user.id, user.email, user.display_name,
    )
