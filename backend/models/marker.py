from enum import Enum
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class Tag(BaseModel):
    """A single subtype tag on a marker. `type` is the parent category id,
    `key` is the subtype icon key (e.g. 'narrow_overgrown_path'),
    `label` is the human-readable label."""
    type: str
    key: Optional[str] = None
    label: str


class MarkerType(str, Enum):
    gate = "gate"
    kissing_gate = "kissing_gate"
    stile = "stile"
    steep = "steep"
    mud = "mud"
    narrow = "narrow"
    rough_surface = "rough_surface"
    field = "field"
    passage = "passage"
    other = "other"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class MarkerCreate(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    type: MarkerType
    subtype: Optional[str] = None
    subtypes: list[Tag] = []
    severity: Optional[Severity] = None
    note: Optional[str] = Field(None, max_length=1000)


class MarkerResponse(BaseModel):
    id: int
    lat: float
    lng: float
    type: MarkerType
    subtype: Optional[str] = None
    subtypes: list[Tag] = []
    severity: Optional[Severity]
    note: Optional[str]
    photo_url: Optional[str]
    source: str
    created_at: datetime
    updated_at: datetime
    confirmation_count: int = 0

    class Config:
        from_attributes = True


class ConfirmationCreate(BaseModel):
    note: Optional[str] = Field(None, max_length=500)
    still_valid: bool = True
