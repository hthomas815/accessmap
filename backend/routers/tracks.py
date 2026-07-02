import json
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from asyncpg import Connection
from pydantic import BaseModel

from auth import AuthUser, ensure_profile, get_current_user
from db import get_db

router = APIRouter(prefix="/tracks", tags=["tracks"])

# Deduplication buffer: ~20 metres at mid-latitudes (0.0002 degrees ≈ 22 m)
DEDUP_BUFFER_DEG = 0.0002


class TrackPoint(BaseModel):
    lat: float
    lng: float


class TrackCreate(BaseModel):
    name: Optional[str] = None
    points: list[TrackPoint]
    gpx_source: Optional[str] = "gpx"


class TrackResponse(BaseModel):
    id: int
    name: Optional[str]
    points: list[TrackPoint]
    gpx_source: Optional[str]
    recorded_at: str


@router.post("", status_code=201)
async def create_track(
    body: TrackCreate,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    if len(body.points) < 2:
        raise HTTPException(status_code=400, detail="Track needs at least 2 points")
    await ensure_profile(db, user)
    return await save_track_points(db, body.name, body.points, body.gpx_source, user.id)


async def save_track_points(db: Connection, name, points, gpx_source="gpx", user_id: str | None = None):
    """Save a list of points as a track, deduplicating against existing coverage.
    `points` is a list of objects/dicts with .lat/.lng (or ['lat','lng']).
    Reused by the GPX upload endpoint and the Strava importer."""
    def _lat(p):
        return p.lat if hasattr(p, "lat") else p["lat"]
    def _lng(p):
        return p.lng if hasattr(p, "lng") else p["lng"]

    if len(points) < 2:
        raise HTTPException(status_code=400, detail="Track needs at least 2 points")

    coords = ", ".join(f"{_lng(p)} {_lat(p)}" for p in points)
    new_wkt = f"LINESTRING({coords})"

    # ── Deduplicate against existing coverage ────────────────────────────────
    if user_id:
        existing = await db.fetchval("SELECT ST_Union(path) FROM tracks WHERE user_id = $1", user_id)
    else:
        existing = await db.fetchval("SELECT ST_Union(path) FROM tracks")

    if existing is None:
        # No existing tracks — save the full upload
        row = await db.fetchrow(
            """
            INSERT INTO tracks (name, path, gpx_source, user_id)
            VALUES ($1, ST_GeomFromText($2, 4326), $3, $4)
            RETURNING id, name, gpx_source, recorded_at,
                      ST_Length(path::geography) / 1000.0 AS km
            """,
            name, new_wkt, gpx_source, user_id,
        )
        return {
            "segments_saved": 1,
            "km_new": round(float(row["km"]), 2),
            "km_skipped": 0.0,
            "message": "Track saved.",
        }

    # Compute new portions: parts of the upload not within the buffer
    result = await db.fetchrow(
        """
        WITH new_geom AS (SELECT ST_GeomFromText($1, 4326) AS g),
             buf      AS (SELECT ST_Buffer(ST_Union($2::geometry), $3) AS g)
        SELECT
            ST_Difference(new_geom.g, buf.g)       AS new_parts,
            ST_Length(new_geom.g::geography) / 1000 AS km_total
        FROM new_geom, buf
        """,
        new_wkt, existing, DEDUP_BUFFER_DEG,
    )

    km_total = float(result["km_total"])
    new_parts = result["new_parts"]  # WKB bytes or None

    if new_parts is None:
        return {
            "segments_saved": 0,
            "km_new": 0.0,
            "km_skipped": round(km_total, 2),
            "message": "This route is already fully covered.",
        }

    # Extract individual LineString components from the difference geometry
    geojson_str = await db.fetchval(
        "SELECT ST_AsGeoJSON($1::geometry)", new_parts
    )
    gj = json.loads(geojson_str)
    geom_type = gj.get("type", "")

    # Collect all coordinate arrays
    if geom_type == "LineString":
        all_lines = [gj["coordinates"]]
    elif geom_type in ("MultiLineString", "GeometryCollection"):
        key = "coordinates" if geom_type == "MultiLineString" else "geometries"
        if geom_type == "GeometryCollection":
            all_lines = [g["coordinates"] for g in gj.get("geometries", [])
                         if g.get("type") == "LineString" and len(g.get("coordinates", [])) >= 2]
        else:
            all_lines = [c for c in gj.get("coordinates", []) if len(c) >= 2]
    else:
        all_lines = []

    if not all_lines:
        return {
            "segments_saved": 0,
            "km_new": 0.0,
            "km_skipped": round(km_total, 2),
            "message": "This route is already fully covered.",
        }

    km_new = 0.0
    for i, line_coords in enumerate(all_lines):
        if len(line_coords) < 2:
            continue
        seg_wkt = "LINESTRING(" + ", ".join(f"{c[0]} {c[1]}" for c in line_coords) + ")"
        seg_name = f"{name} (part {i+1})" if name and len(all_lines) > 1 else name
        km_row = await db.fetchrow(
            """
            INSERT INTO tracks (name, path, gpx_source, user_id)
            VALUES ($1, ST_GeomFromText($2, 4326), $3, $4)
            RETURNING ST_Length(path::geography) / 1000.0 AS km
            """,
            seg_name, seg_wkt, gpx_source, user_id,
        )
        km_new += float(km_row["km"])

    km_skipped = max(0.0, km_total - km_new)
    msg = f"Added {km_new:.1f} km of new coverage."
    if km_skipped > 0.05:
        msg += f" Skipped {km_skipped:.1f} km already explored."

    return {
        "segments_saved": len(all_lines),
        "km_new": round(km_new, 2),
        "km_skipped": round(km_skipped, 2),
        "message": msg,
    }


@router.get("/stats")
async def track_stats(
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """Return personal and community unique distance totals."""
    km_me = await db.fetchval(
        "SELECT COALESCE(ST_Length(ST_Union(path)::geography) / 1000.0, 0) FROM tracks WHERE user_id = $1",
        user.id,
    )
    km_all = await db.fetchval(
        "SELECT COALESCE(ST_Length(ST_Union(path)::geography) / 1000.0, 0) FROM tracks"
    )
    return {"km_me": round(float(km_me), 2), "km_all": round(float(km_all), 2)}


@router.get("")
async def list_tracks(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
    scope: str = Query("me", pattern="^(me|all|both)$"),
):
    where_scope = "TRUE" if scope in ("all", "both") else "user_id = $5"
    rows = await db.fetch(
        f"""
        SELECT
            id,
            name,
            gpx_source,
            recorded_at,
            ST_AsGeoJSON(path) AS geojson,
            COALESCE(user_id = $5, FALSE) AS is_mine
        FROM tracks
        WHERE path && ST_MakeEnvelope($1, $2, $3, $4, 4326)
          AND {where_scope}
        ORDER BY recorded_at DESC
        """,
        min_lng, min_lat, max_lng, max_lat, user.id,
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "gpx_source": r["gpx_source"],
            "recorded_at": r["recorded_at"].isoformat(),
            "geojson": r["geojson"],
            "is_mine": bool(r["is_mine"]),
        }
        for r in rows
    ]
