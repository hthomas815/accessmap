import os
import json
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from asyncpg import Connection

from db import get_db

router = APIRouter(prefix="/routes", tags=["routes"])

# OSRM public demo — no key needed. Swap for self-hosted via env var later.
OSRM_URL = os.getenv("OSRM_URL", "http://router.project-osrm.org")


class LatLng(BaseModel):
    lat: float
    lng: float


class RouteRequest(BaseModel):
    start: LatLng
    end: LatLng
    buffer_m: int = 50
    # Phase 2 hook: pass ["high"] or ["high","medium"] to filter which markers
    # will eventually be passed as avoid_polygons to the routing engine.
    avoid_severities: list[str] = []


@router.post("/preview")
async def preview_route(body: RouteRequest, db: Connection = Depends(get_db)):
    """
    Get a walking route between two points and find accessibility markers near it.

    Phase 1: returns OSRM route + PostGIS proximity query results.
    Phase 2 (avoid_severities wired but not yet passed to router):
      when avoid_severities is set the nearby_markers response already
      flags which markers would be avoided, ready for the next phase.
    """

    # ── 1. Fetch route from OSRM ──────────────────────────────────────────────
    coord_str = f"{body.start.lng},{body.start.lat};{body.end.lng},{body.end.lat}"
    url = f"{OSRM_URL}/route/v1/foot/{coord_str}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                url,
                params={"overview": "full", "geometries": "geojson"},
            )
            r.raise_for_status()
            data = r.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Routing service timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Routing service unavailable: {exc}")

    if data.get("code") != "Ok" or not data.get("routes"):
        raise HTTPException(status_code=422, detail="No route found between these points")

    route = data["routes"][0]
    geometry = route["geometry"]          # GeoJSON LineString
    geometry_json = json.dumps(geometry)

    # ── 2. Find markers near the route via PostGIS ────────────────────────────
    rows = await db.fetch(
        """
        SELECT
            m.id,
            ST_Y(m.location)  AS lat,
            ST_X(m.location)  AS lng,
            m.type, m.subtype, m.severity, m.note,
            ROUND(
                ST_Distance(
                    m.location::geography,
                    ST_GeomFromGeoJSON($1)::geography
                )::numeric,
                1
            ) AS distance_from_route_m
        FROM markers m
        WHERE
            m.archived = FALSE
            AND ST_DWithin(
                m.location::geography,
                ST_GeomFromGeoJSON($1)::geography,
                $2
            )
        ORDER BY distance_from_route_m
        """,
        geometry_json,
        float(body.buffer_m),
    )

    nearby = []
    for r in rows:
        m = dict(r)
        # Phase 2 flag: mark whether this marker would be avoided
        m["would_avoid"] = (
            m.get("severity") in body.avoid_severities
            if body.avoid_severities
            else False
        )
        nearby.append(m)

    return {
        "geometry": geometry,
        "distance_m": route["distance"],
        "duration_s": route["duration"],
        "nearby_markers": nearby,
    }
