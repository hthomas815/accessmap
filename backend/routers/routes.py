import os
import json
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from asyncpg import Connection

from auth import AuthUser, get_current_user
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
    gap_bypass_m: int = 50             # auto-route through a passage gap this close to an avoided obstacle
    gap_suggest_m: int = 100           # offer (don't auto-use) passage gaps this close as an option
    force_gap_ids: list[int] = []      # passage gaps the user explicitly chose to route through

# Walking pace used to derive duration for straight off-path bypass segments.
WALK_SPEED_MS = 1.4


# ── Helpers ───────────────────────────────────────────────────────────────────

async def get_nearby_markers(db: Connection, geometry_json: str, buffer_m: float) -> list[dict]:
    rows = await db.fetch(
        """
        SELECT
            m.id,
            ST_Y(m.location)  AS lat,
            ST_X(m.location)  AS lng,
            m.type, m.subtype, m.subtypes, m.severity, m.note,
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
    nearby = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("subtypes"), str):
            try:
                item["subtypes"] = json.loads(item["subtypes"])
            except ValueError:
                item["subtypes"] = []
        nearby.append(item)
    return nearby


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
          AND severity::text = ANY($2::text[])
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


async def find_bypass_pairs(
    db: Connection, route_geojson: str, severities: list[str],
    avoid_radius_m: int, gap_bypass_m: int,
    force_gap_ids: list[int] | None = None, gap_suggest_m: int = 100,
) -> list[dict]:
    """
    Find avoided obstacles that sit on the route and have an accessible passage
    gap within `gap_bypass_m` (auto), or a user-chosen gap in `force_gap_ids`
    within the wider `gap_suggest_m`. Each returned pair is an obstacle + its
    nearest qualifying gap, ordered by position along the route (start → end).
    """
    if not severities:
        return []
    force_gap_ids = force_gap_ids or []
    rows = await db.fetch(
        """
        WITH rte AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON($1), 4326) AS g)
        SELECT
            o.id                                   AS obs_id,
            ST_Y(o.location)                       AS obs_lat,
            ST_X(o.location)                       AS obs_lng,
            ST_LineLocatePoint((SELECT g FROM rte), o.location) AS obs_frac,
            p.id                                   AS gap_id,
            ST_Y(p.location)                       AS gap_lat,
            ST_X(p.location)                       AS gap_lng,
            ST_Distance(o.location::geography, p.location::geography) AS gap_dist_m
        FROM markers o
        CROSS JOIN rte
        JOIN LATERAL (
            SELECT pp.id, pp.location
            FROM markers pp
            WHERE pp.type = 'passage'
              AND pp.archived = FALSE
              AND (
                    ST_DWithin(pp.location::geography, o.location::geography, $4)
                 OR (pp.id = ANY($5::int[])
                     AND ST_DWithin(pp.location::geography, o.location::geography, $6))
              )
            ORDER BY (pp.id = ANY($5::int[])) DESC, pp.location <-> o.location
            LIMIT 1
        ) p ON TRUE
        WHERE o.archived = FALSE
          AND o.severity::text = ANY($2::text[])
          AND ST_DWithin(o.location::geography, rte.g::geography, $3)
        ORDER BY obs_frac
        """,
        route_geojson, severities, float(avoid_radius_m),
        float(gap_bypass_m), force_gap_ids, float(gap_suggest_m),
    )
    return [dict(r) for r in rows]


async def build_single_bypass(
    db: Connection, route_geojson: str,
    obs_lat: float, obs_lng: float, gap_lat: float, gap_lng: float,
) -> dict | None:
    """
    Rebuild the stretch of `route` crossing an obstacle so it detours through a
    gap: keep the path up to the point nearest the gap before the obstacle,
    straight-line to the gap, then straight-line to the path point nearest the
    gap after the obstacle. Returns the composite coords, the two straight
    bypass segments, and the added straight-line distance — or None if the
    anchors collapse (degenerate case).
    """
    row = await db.fetchrow(
        """
        WITH r AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON($1), 4326) AS g),
        p AS (
            SELECT g,
                   ST_SetSRID(ST_MakePoint($3, $2), 4326) AS obs,
                   ST_SetSRID(ST_MakePoint($5, $4), 4326) AS gap
            FROM r
        ),
        f AS (SELECT g, gap, ST_LineLocatePoint(g, obs) AS obs_frac FROM p),
        a AS (
            SELECT g, gap, obs_frac,
                   ST_ClosestPoint(ST_LineSubstring(g, 0, obs_frac), gap) AS near_pt,
                   ST_ClosestPoint(ST_LineSubstring(g, obs_frac, 1), gap) AS far_pt
            FROM f
        ),
        ff AS (
            SELECT g, gap, near_pt, far_pt,
                   ST_LineLocatePoint(g, near_pt) AS near_frac,
                   ST_LineLocatePoint(g, far_pt)  AS far_frac
            FROM a
        )
        SELECT
            ST_AsGeoJSON(ST_LineSubstring(g, 0, near_frac)) AS before_geo,
            ST_AsGeoJSON(ST_LineSubstring(g, far_frac, 1))  AS after_geo,
            ST_AsGeoJSON(near_pt) AS near_geo,
            ST_AsGeoJSON(far_pt)  AS far_geo,
            ST_AsGeoJSON(gap)     AS gap_geo,
            ST_Distance(near_pt::geography, gap::geography) AS d1,
            ST_Distance(gap::geography, far_pt::geography)  AS d2,
            near_frac, far_frac
        FROM ff
        """,
        route_geojson, obs_lat, obs_lng, gap_lat, gap_lng,
    )
    if row is None or row["near_frac"] is None or row["far_frac"] is None:
        return None
    if float(row["near_frac"]) >= float(row["far_frac"]):
        return None  # anchors collapsed — obstacle at an endpoint, skip

    before_geo = json.loads(row["before_geo"])
    after_geo  = json.loads(row["after_geo"])
    # LineSubstring degenerates to a Point when an anchor lands on a route end.
    if before_geo.get("type") != "LineString" or after_geo.get("type") != "LineString":
        return None
    before = before_geo["coordinates"]
    after  = after_geo["coordinates"]
    near   = json.loads(row["near_geo"])["coordinates"]
    far    = json.loads(row["far_geo"])["coordinates"]
    gap    = json.loads(row["gap_geo"])["coordinates"]
    if len(before) < 2 or len(after) < 2:
        return None

    return {
        "coords":   before + [gap] + after,
        "segments": [[near, gap], [gap, far]],
        "added_m":  float(row["d1"]) + float(row["d2"]),
    }


async def find_suggested_gaps(
    db: Connection, route_geojson: str, severities: list[str],
    avoid_radius_m: int, suggest_m: int, exclude_ids: set[int],
) -> list[dict]:
    """
    Passage gaps within `suggest_m` of an avoided obstacle on the route, other
    than those already auto-used for a bypass. Surfaced as a "nice option"
    (e.g. a pleasant walk along a field edge).
    """
    if not severities:
        return []
    rows = await db.fetch(
        """
        WITH rte AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON($1), 4326) AS g),
        obs AS (
            SELECT location FROM markers, rte
            WHERE archived = FALSE
              AND severity::text = ANY($2::text[])
              AND ST_DWithin(location::geography, rte.g::geography, $3)
        )
        SELECT p.id,
               ST_Y(p.location) AS lat,
               ST_X(p.location) AS lng,
               ROUND(MIN(ST_Distance(p.location::geography, obs.location::geography))::numeric, 1) AS distance_m
        FROM markers p, obs
        WHERE p.type = 'passage'
          AND p.archived = FALSE
          AND ST_DWithin(p.location::geography, obs.location::geography, $4)
        GROUP BY p.id, p.location
        ORDER BY distance_m
        """,
        route_geojson, severities, float(avoid_radius_m), float(suggest_m),
    )
    return [dict(r) for r in rows if r["id"] not in exclude_ids]


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
async def preview_route(
    body: RouteRequest,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """
    Get a walking route between two points.

    Avoidance behaviour (when avoid_severities is non-empty):
      • ORS_API_KEY set   → true polygon avoidance via OpenRouteService
      • ORS_API_KEY unset → request OSRM alternatives, return the one with the
                            fewest avoided-severity markers nearby
    """
    avoidance_applied = False
    avoidance_method: str | None = None
    bypass_segments: list = []       # straight off-path connectors across gaps
    suggested_gaps: list = []        # nearby gaps offered as a nice option (not auto-used)

    # ── Build avoid polygons if needed ────────────────────────────────────────
    avoid_polygons = None
    if body.avoid_severities:
        avoid_polygons = await get_avoid_polygons(
            db, body.avoid_severities, body.avoid_radius_m
        )

    # ── Fetch route ───────────────────────────────────────────────────────────
    try:
        if body.avoid_severities:
            # Accessibility-blind base route — the bypass logic anchors against it.
            base = (await route_via_osrm(body.start, body.end))[0]
            base_geojson = json.dumps(base["geometry"])

            # 1) Prefer a straight-line bypass through a marked accessible gap
            #    sitting within gap_bypass_m of an avoided obstacle on the route.
            pairs = await find_bypass_pairs(
                db, base_geojson, body.avoid_severities,
                body.avoid_radius_m, body.gap_bypass_m,
                body.force_gap_ids, body.gap_suggest_m,
            )
            used_gap_ids: set[int] = set()
            geom = base["geometry"]
            for pr in pairs:
                if pr["gap_id"] in used_gap_ids:
                    continue
                bp = await build_single_bypass(
                    db, json.dumps(geom),
                    pr["obs_lat"], pr["obs_lng"], pr["gap_lat"], pr["gap_lng"],
                )
                if bp is None:
                    continue
                geom = {"type": "LineString", "coordinates": bp["coords"]}
                bypass_segments.extend(bp["segments"])
                used_gap_ids.add(pr["gap_id"])

            if used_gap_ids:
                length_row = await db.fetchrow(
                    "SELECT ST_Length(ST_SetSRID(ST_GeomFromGeoJSON($1), 4326)::geography) AS len",
                    json.dumps(geom),
                )
                dist = float(length_row["len"])
                route_data = {
                    "geometry":   geom,
                    "distance_m": dist,
                    "duration_s": dist / WALK_SPEED_MS,
                }
                avoidance_applied = True
                avoidance_method = "gap_bypass"

            # 2) No usable gap → standard avoidance (ORS polygons or OSRM alternatives).
            elif ORS_API_KEY:
                route_data = await route_via_ors(body.start, body.end, avoid_polygons)
                avoidance_applied = avoid_polygons is not None
                avoidance_method = "ors_avoid_polygons"
            else:
                try:
                    candidates = await route_via_osrm(body.start, body.end, alternatives=True)
                except Exception:
                    candidates = await route_via_osrm(body.start, body.end, alternatives=False)

                best = candidates[0]
                best_score = float("inf")
                if len(candidates) > 1:
                    for candidate in candidates:
                        try:
                            geom_json = json.dumps(candidate["geometry"])
                            row = await db.fetchrow(
                                """
                                SELECT COUNT(*) AS cnt FROM markers
                                WHERE archived = FALSE
                                  AND severity::text = ANY($1::text[])
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
                            score = int(row["cnt"])
                            if score < best_score:
                                best_score = score
                                best = candidate
                        except Exception:
                            continue  # skip this candidate if scoring fails
                route_data = best
                avoidance_method = f"osrm_best_of_{len(candidates)}"

            # Offer other nearby gaps (within gap_suggest_m) as a nice alternative.
            suggested_gaps = await find_suggested_gaps(
                db, base_geojson, body.avoid_severities,
                body.avoid_radius_m, body.gap_suggest_m, used_gap_ids,
            )

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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Routing failed: {exc}")

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
        "bypass_segments":   bypass_segments,   # [[ [lng,lat], [lng,lat] ], ...] straight gap crossings
        "suggested_gaps":    suggested_gaps,    # nearby passage gaps offered as a nice option
    }
