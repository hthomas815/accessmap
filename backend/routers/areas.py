from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from asyncpg import Connection
from pydantic import BaseModel

from auth import AuthUser, ensure_profile, get_current_user
from db import get_db

router = APIRouter(prefix="/areas", tags=["areas"])


class AreaCreate(BaseModel):
    osm_type: Optional[str] = None      # 'way' | 'relation'
    osm_id: Optional[int] = None
    name: Optional[str] = None
    kind: Optional[str] = None          # e.g. 'park', 'recreation_ground'
    geojson: str                        # GeoJSON geometry: Polygon or MultiPolygon


@router.get("")
async def list_areas(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """All of the current user's claimed areas intersecting the viewport."""
    rows = await db.fetch(
        """
        SELECT id, name, kind, osm_type, osm_id,
               ST_AsGeoJSON(area) AS geojson,
               ST_Area(area::geography) / 1e6 AS km2
        FROM claimed_areas
        WHERE user_id = $5
          AND area && ST_MakeEnvelope($1, $2, $3, $4, 4326)
        """,
        min_lng, min_lat, max_lng, max_lat, user.id,
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "osm_type": r["osm_type"],
            "osm_id": r["osm_id"],
            "geojson": r["geojson"],
            "km2": float(r["km2"] or 0),
        }
        for r in rows
    ]


@router.get("/list")
async def list_all_areas(
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """Every area the user has claimed (for the Explored dashboard list)."""
    rows = await db.fetch(
        """
        SELECT id, name, kind, osm_type, osm_id,
               ST_Area(area::geography) / 1e6 AS km2
        FROM claimed_areas
        WHERE user_id = $1
        ORDER BY created_at DESC
        """,
        user.id,
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "km2": float(r["km2"] or 0),
        }
        for r in rows
    ]


@router.post("", status_code=201)
async def create_area(
    body: AreaCreate,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """Claim an area. Idempotent per (user, osm_type, osm_id)."""
    await ensure_profile(db, user)
    try:
        row = await db.fetchrow(
            """
            INSERT INTO claimed_areas (user_id, osm_type, osm_id, name, kind, area)
            VALUES (
                $1, $2, $3, $4, $5,
                ST_Multi(ST_CollectionExtract(
                    ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON($6), 4326)), 3))
            )
            ON CONFLICT (user_id, osm_type, osm_id) WHERE osm_id IS NOT NULL
              DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind, area = EXCLUDED.area
            RETURNING id, ST_AsGeoJSON(area) AS geojson,
                      ST_Area(area::geography) / 1e6 AS km2
            """,
            user.id, body.osm_type, body.osm_id, body.name, body.kind, body.geojson,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid area geometry: {exc}")
    if not row:
        raise HTTPException(status_code=400, detail="Could not save area")
    return {
        "id": row["id"],
        "name": body.name,
        "kind": body.kind,
        "osm_type": body.osm_type,
        "osm_id": body.osm_id,
        "geojson": row["geojson"],
        "km2": float(row["km2"] or 0),
    }


@router.delete("/{area_id}")
async def delete_area(
    area_id: int,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    await db.execute(
        "DELETE FROM claimed_areas WHERE id = $1 AND user_id = $2",
        area_id, user.id,
    )
    return {"ok": True}


@router.delete("/by-osm/{osm_type}/{osm_id}")
async def delete_area_by_osm(
    osm_type: str,
    osm_id: int,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """Un-claim by OSM identity — used by the tap-to-toggle flow."""
    await db.execute(
        "DELETE FROM claimed_areas WHERE user_id = $1 AND osm_type = $2 AND osm_id = $3",
        user.id, osm_type, osm_id,
    )
    return {"ok": True}
