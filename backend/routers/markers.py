import os
import base64
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from asyncpg import Connection

from pydantic import BaseModel
from models.marker import MarkerCreate, MarkerResponse, MarkerType, Severity, ConfirmationCreate


class CommentCreate(BaseModel):
    body: str


class MarkerUpdate(BaseModel):
    type: Optional[MarkerType] = None
    subtype: Optional[str] = None
    severity: Optional[Severity] = None
    note: Optional[str] = None
from db import get_db

router = APIRouter(prefix="/markers", tags=["markers"])


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
        try:
            content = await photo.read()
            ext = os.path.splitext(photo.filename or "")[1].lower().lstrip(".") or "jpeg"
            mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
            b64 = base64.b64encode(content).decode("ascii")
            photo_url = f"data:{mime};base64,{b64}"
        except Exception as exc:
            # Non-fatal: save marker without photo rather than crashing
            print(f"Photo encode failed (marker saved without photo): {exc}")

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


@router.patch("/{marker_id}", response_model=MarkerResponse)
async def update_marker(marker_id: int, body: MarkerUpdate, db: Connection = Depends(get_db)):
    row = await db.fetchrow(
        "SELECT * FROM markers WHERE id = $1 AND archived = FALSE", marker_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Marker not found")

    new_type     = body.type.value     if body.type     is not None else row["type"]
    new_subtype  = body.subtype        if body.subtype  is not None else row["subtype"]
    new_severity = body.severity.value if body.severity is not None else row["severity"]
    new_note     = body.note           if body.note     is not None else row["note"]

    updated = await db.fetchrow(
        """
        UPDATE markers
        SET type = $1, subtype = $2, severity = $3, note = $4
        WHERE id = $5
        RETURNING
            id,
            ST_Y(location) AS lat,
            ST_X(location) AS lng,
            type, subtype, severity, note, photo_url, source, created_at, updated_at
        """,
        new_type, new_subtype, new_severity, new_note, marker_id,
    )
    count = await db.fetchval("SELECT COUNT(*) FROM confirmations WHERE marker_id = $1", marker_id)
    return {**dict(updated), "confirmation_count": count}


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


@router.get("/{marker_id}/comments")
async def list_comments(marker_id: int, db: Connection = Depends(get_db)):
    exists = await db.fetchval(
        "SELECT id FROM markers WHERE id = $1 AND archived = FALSE", marker_id
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Marker not found")

    rows = await db.fetch(
        "SELECT id, body, created_at FROM comments WHERE marker_id = $1 ORDER BY created_at ASC",
        marker_id,
    )
    return [{"id": r["id"], "body": r["body"], "created_at": r["created_at"].isoformat()} for r in rows]


@router.post("/{marker_id}/comments", status_code=201)
async def add_comment(marker_id: int, body: CommentCreate, db: Connection = Depends(get_db)):
    exists = await db.fetchval(
        "SELECT id FROM markers WHERE id = $1 AND archived = FALSE", marker_id
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Marker not found")

    row = await db.fetchrow(
        "INSERT INTO comments (marker_id, body) VALUES ($1, $2) RETURNING id, body, created_at",
        marker_id, body.body.strip(),
    )
    return {"id": row["id"], "body": row["body"], "created_at": row["created_at"].isoformat()}
