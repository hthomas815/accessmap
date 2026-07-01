from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from asyncpg import Connection
from pydantic import BaseModel

from db import get_db

router = APIRouter(prefix="/tracks", tags=["tracks"])


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
async def create_track(body: TrackCreate, db: Connection = Depends(get_db)):
    if len(body.points) < 2:
        raise HTTPException(status_code=400, detail="Track needs at least 2 points")

    # Build WKT LineString from points
    coords = ", ".join(f"{p.lng} {p.lat}" for p in body.points)
    wkt = f"LINESTRING({coords})"

    row = await db.fetchrow(
        """
        INSERT INTO tracks (name, path, gpx_source)
        VALUES ($1, ST_GeomFromText($2, 4326), $3)
        RETURNING id, name, gpx_source, recorded_at
        """,
        body.name, wkt, body.gpx_source,
    )
    return {"id": row["id"], "point_count": len(body.points)}


@router.get("")
async def list_tracks(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    db: Connection = Depends(get_db),
):
    rows = await db.fetch(
        """
        SELECT
            id,
            name,
            gpx_source,
            recorded_at,
            ST_AsGeoJSON(path) AS geojson
        FROM tracks
        WHERE path && ST_MakeEnvelope($1, $2, $3, $4, 4326)
        ORDER BY recorded_at DESC
        """,
        min_lng, min_lat, max_lng, max_lat,
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "gpx_source": r["gpx_source"],
            "recorded_at": r["recorded_at"].isoformat(),
            "geojson": r["geojson"],
        }
        for r in rows
    ]
