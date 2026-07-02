import os
import json
import base64
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from asyncpg import Connection

from auth import AuthUser, ensure_profile, get_current_user
from pydantic import BaseModel
from models.marker import MarkerCreate, MarkerResponse, MarkerType, Severity, ConfirmationCreate, Tag


class CommentCreate(BaseModel):
    body: str


class MarkerUpdate(BaseModel):
    type: Optional[MarkerType] = None
    subtype: Optional[str] = None
    subtypes: Optional[list[Tag]] = None
    severity: Optional[Severity] = None
    note: Optional[str] = None
from db import get_db

router = APIRouter(prefix="/markers", tags=["markers"])


def _parse_subtypes(val):
    """asyncpg returns jsonb as a str; normalise to a list of tag dicts."""
    if val is None:
        return []
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    if isinstance(val, list):
        return val
    return []


@router.post("", response_model=MarkerResponse, status_code=201)
async def create_marker(
    lat: float = Form(...),
    lng: float = Form(...),
    type: MarkerType = Form(...),
    subtype: Optional[str] = Form(None),
    subtypes: Optional[str] = Form(None),
    severity: Optional[Severity] = Form(None),
    note: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    await ensure_profile(db, user)
    # subtypes arrives as a JSON string (multipart form). Parse to a list of tags.
    try:
        tags = json.loads(subtypes) if subtypes else []
        if not isinstance(tags, list):
            tags = []
    except (ValueError, TypeError):
        tags = []
    # Backfill legacy single `subtype` from first tag if not supplied
    if not subtype and tags:
        subtype = tags[0].get("label") if isinstance(tags[0], dict) else None

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
        INSERT INTO markers (location, type, subtype, subtypes, severity, note, photo_url, user_id)
        VALUES (ST_SetSRID(ST_MakePoint($1, $2), 4326), $3, $4, $5::jsonb, $6, $7, $8, $9)
        RETURNING
            id,
            ST_Y(location) AS lat,
            ST_X(location) AS lng,
            type, subtype, subtypes, severity, note, photo_url, source, created_at, updated_at
        """,
        lng, lat, type.value, subtype, json.dumps(tags),
        severity.value if severity else None, note, photo_url, user.id,
    )
    return {
        **dict(row),
        "subtypes": _parse_subtypes(row["subtypes"]),
        "confirmation_count": 0,
        "created_by_me": True,
    }


@router.get("", response_model=list[MarkerResponse])
async def list_markers(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    rows = await db.fetch(
        """
        SELECT
            m.id,
            ST_Y(m.location) AS lat,
            ST_X(m.location) AS lng,
            m.type, m.subtype, m.subtypes, m.severity, m.note, m.photo_url, m.source,
            m.created_at, m.updated_at,
            COUNT(c.id) AS confirmation_count,
            COALESCE(m.user_id = $5, FALSE) AS created_by_me
        FROM markers m
        LEFT JOIN confirmations c ON c.marker_id = m.id
        WHERE
            m.archived = FALSE
            AND m.location && ST_MakeEnvelope($1, $2, $3, $4, 4326)
        GROUP BY m.id
        ORDER BY m.created_at DESC
        """,
        min_lng, min_lat, max_lng, max_lat, user.id,
    )
    return [{**dict(r), "subtypes": _parse_subtypes(r["subtypes"])} for r in rows]


@router.get("/{marker_id}", response_model=MarkerResponse)
async def get_marker(
    marker_id: int,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    row = await db.fetchrow(
        """
        SELECT
            m.id,
            ST_Y(m.location) AS lat,
            ST_X(m.location) AS lng,
            m.type, m.subtype, m.subtypes, m.severity, m.note, m.photo_url, m.source,
            m.created_at, m.updated_at,
            COUNT(c.id) AS confirmation_count,
            COALESCE(m.user_id = $2, FALSE) AS created_by_me
        FROM markers m
        LEFT JOIN confirmations c ON c.marker_id = m.id
        WHERE m.id = $1 AND m.archived = FALSE
        GROUP BY m.id
        """,
        marker_id, user.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Marker not found")
    return {**dict(row), "subtypes": _parse_subtypes(row["subtypes"])}


@router.patch("/{marker_id}", response_model=MarkerResponse)
async def update_marker(
    marker_id: int,
    body: MarkerUpdate,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    row = await db.fetchrow(
        "SELECT * FROM markers WHERE id = $1 AND archived = FALSE", marker_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Marker not found")
    if row["user_id"] and row["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own markers.")

    new_type     = body.type.value     if body.type     is not None else row["type"]
    new_severity = body.severity.value if body.severity is not None else row["severity"]
    new_note     = body.note           if body.note     is not None else row["note"]

    if body.subtypes is not None:
        new_tags = [t.model_dump() for t in body.subtypes]
        new_subtype = new_tags[0]["label"] if new_tags else None
    else:
        new_tags = _parse_subtypes(row["subtypes"])
        new_subtype = body.subtype if body.subtype is not None else row["subtype"]

    updated = await db.fetchrow(
        """
        UPDATE markers
        SET type = $1, subtype = $2, subtypes = $3::jsonb, severity = $4, note = $5
        WHERE id = $6
        RETURNING
            id,
            ST_Y(location) AS lat,
            ST_X(location) AS lng,
            type, subtype, subtypes, severity, note, photo_url, source, created_at, updated_at
        """,
        new_type, new_subtype, json.dumps(new_tags), new_severity, new_note, marker_id,
    )
    count = await db.fetchval("SELECT COUNT(*) FROM confirmations WHERE marker_id = $1", marker_id)
    return {
        **dict(updated),
        "subtypes": _parse_subtypes(updated["subtypes"]),
        "confirmation_count": count,
        "created_by_me": True,
    }


@router.delete("/{marker_id}", status_code=204)
async def delete_marker(
    marker_id: int,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    marker = await db.fetchrow(
        "SELECT user_id FROM markers WHERE id = $1 AND archived = FALSE",
        marker_id,
    )
    if not marker:
        raise HTTPException(status_code=404, detail="Marker not found")
    owner = marker["user_id"]
    if owner and owner != user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own markers.")
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
    user: AuthUser = Depends(get_current_user),
):
    await ensure_profile(db, user)
    exists = await db.fetchval(
        "SELECT id FROM markers WHERE id = $1 AND archived = FALSE", marker_id
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Marker not found")

    await db.execute(
        """
        INSERT INTO confirmations (marker_id, note, still_valid, user_id)
        VALUES ($1, $2, $3, $4)
        """,
        marker_id, body.note, body.still_valid, user.id,
    )

    if not body.still_valid:
        await db.execute(
            "UPDATE markers SET archived = TRUE WHERE id = $1", marker_id
        )

    return {"status": "ok"}


@router.get("/{marker_id}/comments")
async def list_comments(
    marker_id: int,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    exists = await db.fetchval(
        "SELECT id FROM markers WHERE id = $1 AND archived = FALSE", marker_id
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Marker not found")

    rows = await db.fetch(
        """
        SELECT c.id, c.body, c.created_at, p.display_name, p.email,
               COALESCE(c.user_id = $2, FALSE) AS created_by_me
        FROM comments c
        LEFT JOIN profiles p ON p.id = c.user_id
        WHERE c.marker_id = $1
        ORDER BY c.created_at ASC
        """,
        marker_id, user.id,
    )
    return [
        {
            "id": r["id"],
            "body": r["body"],
            "created_at": r["created_at"].isoformat(),
            "author_name": r["display_name"] or r["email"] or "AccessMap user",
            "created_by_me": bool(r["created_by_me"]),
        }
        for r in rows
    ]


@router.post("/{marker_id}/comments", status_code=201)
async def add_comment(
    marker_id: int,
    body: CommentCreate,
    db: Connection = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    await ensure_profile(db, user)
    exists = await db.fetchval(
        "SELECT id FROM markers WHERE id = $1 AND archived = FALSE", marker_id
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Marker not found")

    row = await db.fetchrow(
        "INSERT INTO comments (marker_id, body, user_id) VALUES ($1, $2, $3) RETURNING id, body, created_at",
        marker_id, body.body.strip(), user.id,
    )
    return {
        "id": row["id"],
        "body": row["body"],
        "created_at": row["created_at"].isoformat(),
        "author_name": user.display_name or user.email or "AccessMap user",
        "created_by_me": True,
    }
