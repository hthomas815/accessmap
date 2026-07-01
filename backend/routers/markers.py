import os
import uuid
import aiofiles
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from asyncpg import Connection

from models.marker import MarkerCreate, MarkerResponse, MarkerType, Severity, ConfirmationCreate
from db import get_db

router = APIRouter(prefix="/markers", tags=["markers"])

UPLOAD_DIR = "/app/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


@router.post("", response_model=MarkerResponse, status_code=201)
async def create_marker(
    lat: float = Form(...),
    lng: float = Form(...),
    type: MarkerType = Form(...),
    subtype: Optional[str] = Form(None),
    severity: Optional[Severity] = Form(None),
    note: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    db: Connection = Depends(get_db),
):
    photo_url = None
    if photo:
        ext = os.path.splitext(photo.filename)[1].lower() or ".jpg"
        filename = f"{uuid.uuid4()}{ext}"
        path = os.path.join(UPLOAD_DIR, filename)
        async with aiofiles.open(path, "wb") as f:
            content = await photo.read()
            await f.write(content)
        photo_url = f"/uploads/{filename}"

    row = await db.fetchrow(
        """
        INSERT INTO markers (location, type, subtype, severity, note, photo_url)
        VALUES (ST_SetSRID(ST_MakePoint($1, $2), 4326), $3, $4, $5, $6, $7)
        RETURNING
            id,
            ST_Y(location) AS lat,
            ST_X(location) AS lng,
            type, subtype, severity, note, photo_url, source, created_at, updated_at
        """,
        lng, lat, type.value, subtype, severity.value if severity else None, note, photo_url,
    )
    return {**dict(row), "confirmation_count": 0}


@router.get("", response_model=list[MarkerResponse])
async def list_markers(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    db: Connection = Depends(get_db),
):
    rows = await db.fetch(
        """
        SELECT
            m.id,
            ST_Y(m.location) AS lat,
            ST_X(m.location) AS lng,
            m.type, m.subtype, m.severity, m.note, m.photo_url, m.source,
            m.created_at, m.updated_at,
            COUNT(c.id) AS confirmation_count
        FROM markers m
        LEFT JOIN confirmations c ON c.marker_id = m.id
        WHERE
            m.archived = FALSE
            AND m.location && ST_MakeEnvelope($1, $2, $3, $4, 4326)
        GROUP BY m.id
        ORDER BY m.created_at DESC
        """,
        min_lng, min_lat, max_lng, max_lat,
    )
    return [dict(r) for r in rows]


@router.get("/{marker_id}", response_model=MarkerResponse)
async def get_marker(marker_id: int, db: Connection = Depends(get_db)):
    row = await db.fetchrow(
        """
        SELECT
            m.id,
            ST_Y(m.location) AS lat,
            ST_X(m.location) AS lng,
            m.type, m.subtype, m.severity, m.note, m.photo_url, m.source,
            m.created_at, m.updated_at,
            COUNT(c.id) AS confirmation_count
        FROM markers m
        LEFT JOIN confirmations c ON c.marker_id = m.id
        WHERE m.id = $1 AND m.archived = FALSE
        GROUP BY m.id
        """,
        marker_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Marker not found")
    return dict(row)


@router.delete("/{marker_id}", status_code=204)
async def delete_marker(marker_id: int, db: Connection = Depends(get_db)):
    result = await db.execute(
        "UPDATE markers SET archived = TRUE WHERE id = $1 AND archived = FALSE",
        marker_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Marker not found")


@router.post("/{marker_id}/confirm", status_code=201)
async def confirm_marker(
    marker_id: int,
    body: ConfirmationCreate,
    db: Connection = Depends(get_db),
):
    exists = await db.fetchval(
        "SELECT id FROM markers WHERE id = $1 AND archived = FALSE", marker_id
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Marker not found")

    await db.execute(
        """
        INSERT INTO confirmations (marker_id, note, still_valid)
        VALUES ($1, $2, $3)
        """,
        marker_id, body.note, body.still_valid,
    )

    if not body.still_valid:
        await db.execute(
            "UPDATE markers SET archived = TRUE WHERE id = $1", marker_id
        )

    return {"status": "ok"}
