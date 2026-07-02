"""Strava sync integration.

Single-user MVP that mirrors the app's no-login model: one connected Strava
account, stored in the `strava_accounts` table. New activities are pulled in as
a *pending* review queue (filtered by activity type) and only added to coverage
when the user taps "Import" — nothing is added automatically.

Setup (one-time, by the app owner):
  1. Create an app at https://www.strava.com/settings/api
  2. Set the "Authorization Callback Domain" to your app's domain
  3. Provide these environment variables to the backend:
       STRAVA_CLIENT_ID
       STRAVA_CLIENT_SECRET
       STRAVA_REDIRECT_URI   (e.g. https://your-app.onrender.com/api/strava/callback)
       APP_BASE_URL          (optional, where to send the user back after connecting)
"""

import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import RedirectResponse
from asyncpg import Connection

from db import get_db
from routers.tracks import save_track_points

router = APIRouter(prefix="/strava", tags=["strava"])

CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("STRAVA_REDIRECT_URI", "")
APP_BASE_URL  = os.getenv("APP_BASE_URL", "/")

# Which Strava activity types are eligible for import by default.
# (Strava's `sport_type` values — walking-style activities only.)
DEFAULT_TYPES = {"Walk", "Hike", "TrailRun", "Run"}

AUTH_URL  = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE  = "https://www.strava.com/api/v3"


def _configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)


async def _get_account(db: Connection):
    return await db.fetchrow("SELECT * FROM strava_accounts ORDER BY connected_at DESC LIMIT 1")


async def _valid_access_token(db: Connection) -> str:
    """Return a non-expired access token, refreshing via the refresh token if needed."""
    acct = await _get_account(db)
    if not acct:
        raise HTTPException(status_code=401, detail="Strava not connected")

    # Refresh if the token expires within the next 2 minutes
    if acct["expires_at"] <= int(time.time()) + 120:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(TOKEN_URL, data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": acct["refresh_token"],
            })
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Strava token refresh failed")
        t = resp.json()
        await db.execute(
            """UPDATE strava_accounts
               SET access_token = $1, refresh_token = $2, expires_at = $3
               WHERE athlete_id = $4""",
            t["access_token"], t["refresh_token"], t["expires_at"], acct["athlete_id"],
        )
        return t["access_token"]

    return acct["access_token"]


@router.get("/status")
async def status(db: Connection = Depends(get_db)):
    if not _configured():
        return {"configured": False, "connected": False,
                "detail": "Strava app credentials not set on the server."}
    acct = await _get_account(db)
    pending = await db.fetchval(
        "SELECT COUNT(*) FROM strava_activities WHERE status = 'pending'"
    ) or 0
    if not acct:
        return {"configured": True, "connected": False, "pending_count": 0}
    return {
        "configured": True,
        "connected": True,
        "athlete": {
            "name": " ".join(filter(None, [acct["firstname"], acct["lastname"]])) or "Athlete",
        },
        "pending_count": pending,
    }


@router.get("/connect")
async def connect():
    """Return the Strava OAuth URL for the app to open."""
    if not _configured():
        raise HTTPException(status_code=400, detail="Strava app credentials not set on the server.")
    url = (
        f"{AUTH_URL}?client_id={CLIENT_ID}"
        f"&response_type=code&redirect_uri={REDIRECT_URI}"
        f"&approval_prompt=auto&scope=read,activity:read_all"
    )
    return {"url": url}


@router.get("/callback")
async def callback(code: str = Query(None), error: str = Query(None),
                   db: Connection = Depends(get_db)):
    """Strava redirects here after the user authorises."""
    if error or not code:
        return RedirectResponse(f"{APP_BASE_URL}?strava=error")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        })
    if resp.status_code != 200:
        return RedirectResponse(f"{APP_BASE_URL}?strava=error")
    t = resp.json()
    ath = t.get("athlete", {}) or {}
    await db.execute(
        """INSERT INTO strava_accounts
               (athlete_id, access_token, refresh_token, expires_at, firstname, lastname)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (athlete_id) DO UPDATE
               SET access_token = EXCLUDED.access_token,
                   refresh_token = EXCLUDED.refresh_token,
                   expires_at = EXCLUDED.expires_at""",
        ath.get("id"), t["access_token"], t["refresh_token"], t["expires_at"],
        ath.get("firstname"), ath.get("lastname"),
    )
    return RedirectResponse(f"{APP_BASE_URL}?strava=connected")


@router.post("/disconnect")
async def disconnect(db: Connection = Depends(get_db)):
    await db.execute("DELETE FROM strava_accounts")
    return {"status": "ok"}


@router.post("/sync")
async def sync(db: Connection = Depends(get_db),
               types: str = Query(None, description="Comma-separated sport_type filter")):
    """Pull recent activities from Strava into the pending review queue.

    Only walking-type activities (or the supplied `types`) are queued, and only
    ones we haven't already seen. Nothing is added to coverage here."""
    token = await _valid_access_token(db)
    wanted = {t.strip() for t in types.split(",")} if types else DEFAULT_TYPES

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{API_BASE}/athlete/activities",
            headers={"Authorization": f"Bearer {token}"},
            params={"per_page": 50, "page": 1},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Strava activities fetch failed ({resp.status_code})")

    added = 0
    for a in resp.json():
        sport = a.get("sport_type") or a.get("type")
        if sport not in wanted:
            continue
        start = a.get("start_date")
        start_dt = None
        if start:
            try:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except ValueError:
                start_dt = None
        # Insert as pending only if we've never seen this activity
        result = await db.execute(
            """INSERT INTO strava_activities
                   (activity_id, athlete_id, name, sport_type, distance_m, start_date, status)
               VALUES ($1, $2, $3, $4, $5, $6, 'pending')
               ON CONFLICT (activity_id) DO NOTHING""",
            a["id"], a.get("athlete", {}).get("id"), a.get("name"), sport,
            a.get("distance"), start_dt,
        )
        if result.endswith("1"):
            added += 1

    pending = await db.fetchval("SELECT COUNT(*) FROM strava_activities WHERE status = 'pending'") or 0
    return {"new": added, "pending_count": pending}


@router.get("/activities")
async def activities(db: Connection = Depends(get_db),
                     status_filter: str = Query("pending", alias="status")):
    rows = await db.fetch(
        """SELECT activity_id, name, sport_type, distance_m, start_date, status
           FROM strava_activities
           WHERE ($1 = 'all' OR status = $1)
           ORDER BY start_date DESC NULLS LAST
           LIMIT 100""",
        status_filter,
    )
    return [
        {
            "id": r["activity_id"],
            "name": r["name"],
            "sport_type": r["sport_type"],
            "distance_km": round((r["distance_m"] or 0) / 1000.0, 2),
            "start_date": r["start_date"].isoformat() if r["start_date"] else None,
            "status": r["status"],
        }
        for r in rows
    ]


@router.post("/activities/{activity_id}/import")
async def import_activity(activity_id: int, db: Connection = Depends(get_db)):
    """Fetch an activity's GPS track from Strava and add it to coverage."""
    token = await _valid_access_token(db)
    row = await db.fetchrow("SELECT * FROM strava_activities WHERE activity_id = $1", activity_id)
    if not row:
        raise HTTPException(status_code=404, detail="Activity not in queue — run sync first")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{API_BASE}/activities/{activity_id}/streams",
            headers={"Authorization": f"Bearer {token}"},
            params={"keys": "latlng", "key_by_type": "true"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Strava stream fetch failed ({resp.status_code})")

    data = resp.json()
    latlng = (data.get("latlng") or {}).get("data") or []
    points = [{"lat": p[0], "lng": p[1]} for p in latlng if isinstance(p, list) and len(p) == 2]
    if len(points) < 2:
        await db.execute("UPDATE strava_activities SET status = 'no_gps' WHERE activity_id = $1", activity_id)
        raise HTTPException(status_code=400, detail="Activity has no GPS track")

    summary = await save_track_points(db, row["name"] or "Strava activity", points, "strava")
    await db.execute("UPDATE strava_activities SET status = 'imported' WHERE activity_id = $1", activity_id)
    return {"status": "imported", **summary}


@router.post("/activities/{activity_id}/dismiss")
async def dismiss_activity(activity_id: int, db: Connection = Depends(get_db)):
    await db.execute("UPDATE strava_activities SET status = 'dismissed' WHERE activity_id = $1", activity_id)
    return {"status": "dismissed"}
