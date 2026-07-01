import os
import json
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from asyncpg import Connection

from db import get_db

router = APIRouter(prefix="/routes", tags=["routes"])

# OSM Community routing server — foot profile.
OSRM_URL = os.getenv("OSRM_URL", "https://routing.openstreetmap.de/routed-foot")

# OpenRouteService — set ORS_API_KEY env var to enable true polygon avoidance.
# Free tier: sign up at https://openrouteservice.org (email only, no card).
ORS_API_KEY = os.getenv("ORS_API_KEY", "")
ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"


# ── Pydantic models ───────────────────────────────────────────────────────────

class LatLng(BaseModel):
    lat: float
    lng: float


class RouteRequest(BaseModel):
    start: LatLng
    end: LatLng
    buffer_m: int = 50
    avoid_severities: list[str] = []   # e.g. ["high"] or ["high", "medium"]
    avoid_radius_m: int = 15           # buffer circle radius around each avoided marker


# ── Helpers ───────────────────────────────────────────────────────────────────

async def get_nearby_markers(db: Connection, geometry_json: str, buffer_m: float) -> list[dict]:
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
                )::numeric, 1
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
        geometry_json, float(buffer_m),
    )
    return [dict(r) for r in rows]


async def get_avoid_polygons(
    db: Connection, severities: list[str], avoid_radius_m: int
) -> dict | None:
    """
    Use PostGIS to generate circular buffer polygons around markers of the
    given severities. Returns a GeoJSON MultiPolygon or None if no markers found.
    """
    if not severities:
        return None

    rows = await db.fetch(
        """
        SELECT ST_AsGeoJSON(
            ST_Buffer(location::geography, $1)::geometry
        ) AS geojson
        FROM markers
        WHERE archived = FALSE
          AND severity = ANY($2::text[])
        """,
        float(avoid_radius_m), severities,
    )

    polygons = []
    for row in rows:
        geom = json.loads(row["geojson"])
        if geom["type"] == "Polygon":
            polygons.append(geom["coordinates"])

    if not polygons:
        return None

    return {"type": "MultiPolygon", "coordinates": polygons}


async def route_via_ors(
    start: LatLng, end: LatLng, avoid_polygons: dict | None = None
) -> dict:
    """
    Call OpenRouteService for a walking route.
    Optionally pass avoid_polygons (GeoJSON MultiPolygon) to route around obstacles.
    """
    body: dict = {
        "coordinates": [
            [start.lng, start.lat],
            [end.lng,   end.lat],
        ]
    }
    if avoid_polygons:
        body["options"] = {"avoid_polygons": avoid_polygons}

    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, application/geo+json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(ORS_URL, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()

    feature  = data["features"][0]
    summary  = feature["properties"]["summary"]
    return {
        "geometry":   feature["geometry"],
        "distance_m": summary["distance"],
        "duration_s": summary["duration"],
    }


async def route_via_osrm(
    start: LatLng, end: LatLng, alternatives: bool = False
) -> list[dict]:
    """
    Call OSRM for a walking route.
    When alternatives=True, requests up to 3 routes so the caller can pick the
    least-obstructed one (fallback when no ORS key).
    """
    coord_str = f"{start.lng},{start.lat};{end.lng},{end.lat}"
    url = f"{OSRM_URL}/route/v1/driving/{coord_str}"
    params: dict = {"overview": "full", "geometries": "geojson"}
    if alternatives:
        params["alternatives"] = "true"

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("No route found between these points")

    return [
        {
            "geometry":   route["geometry"],
            "distance_m": route["distance"],
            "duration_s": route["duration"],
        }
        for route in data["routes"]
    ]


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/preview")
async def preview_route(body: RouteRequest, db: Connection = Depends(get_db)):
    """
    Get a walking route between two points.

    Avoidance behaviour (when avoid_severities is non-empty):
      • ORS_API_KEY set   → true polygon avoidance via OpenRouteService
      • ORS_API_KEY unset → request OSRM alternatives, return the one with the
                            fewest avoided-severity markers nearby
    """
    avoidance_applied = False
    avoidance_method: str | None = None

    # ── Build avoid polygons if needed ────────────────────────────────────────
    avoid_polygons = None
    if body.avoid_severities:
        avoid_polygons = await get_avoid_polygons(
            db, body.avoid_severities, body.avoid_radius_m
        )

    # ── Fetch route ───────────────────────────────────────────────────────────
    try:
        if body.avoid_severities and ORS_API_KEY:
            # True polygon avoidance
            route_data = await route_via_ors(body.start, body.end, avoid_polygons)
            avoidance_applied = avoid_polygons is not None
            avoidance_method = "ors_avoid_polygons"

        elif body.avoid_severities and not ORS_API_KEY:
            # Fallback: OSRM alternatives, pick least-obstructed
            candidates = await route_via_osrm(body.start, body.end, alternatives=True)
            best = candidates[0]
            best_score = float("inf")
            for candidate in candidates:
                geom_json = json.dumps(candidate["geometry"])
                row = await db.fetchrow(
                    """
                    SELECT COUNT(*) AS cnt FROM markers
                    WHERE archived = FALSE
                      AND severity = ANY($1::text[])
                      AND ST_DWithin(
                            location::geography,
                            ST_GeomFromGeoJSON($2)::geography,
                            $3
                          )
                    """,
                    body.avoid_severities,
                    geom_json,
                    float(body.avoid_radius_m * 3),
                )
                score = row["cnt"]
                if score < best_score:
                    best_score = score
                    best = candidate
            route_data = best
            avoidance_method = f"osrm_best_of_{len(candidates)}"

        else:
            routes = await route_via_osrm(body.start, body.end)
            route_data = routes[0]

    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Routing service timed out")
    except httpx.HTTPStatusError as exc:
        detail = f"Routing service error {exc.response.status_code}"
        try:
            detail += f": {exc.response.json().get('error', {}).get('message', '')}"
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=detail)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Routing service unavailable: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # ── Find markers near the chosen route ────────────────────────────────────
    geometry      = route_data["geometry"]
    geometry_json = json.dumps(geometry)

    nearby = await get_nearby_markers(db, geometry_json, body.buffer_m)
    for m in nearby:
        m["would_avoid"] = (
            m.get("severity") in body.avoid_severities
            if body.avoid_severities else False
        )

    return {
        "geometry":          geometry,
        "distance_m":        route_data["distance_m"],
        "duration_s":        route_data["duration_s"],
        "nearby_markers":    nearby,
        "avoidance_applied": avoidance_applied,
        "avoidance_method":  avoidance_method,
    }
