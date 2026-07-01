-- Enable PostGIS
CREATE EXTENSION IF NOT EXISTS postgis;

-- Marker types enum
CREATE TYPE marker_type AS ENUM (
    'gate',
    'kissing_gate',
    'stile',
    'steep',
    'mud',
    'narrow',
    'rough_surface',
    'other'
);

-- Severity enum
CREATE TYPE severity_level AS ENUM (
    'low',       -- passable, minor inconvenience
    'medium',    -- requires effort / care
    'high'       -- likely impassable for some users
);

-- Core markers table
CREATE TABLE markers (
    id              SERIAL PRIMARY KEY,
    location        GEOMETRY(Point, 4326) NOT NULL,
    type            marker_type NOT NULL,
    severity        severity_level,
    note            TEXT,
    photo_url       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- source: 'personal' initially, later 'confirmed' if others validate
    source          TEXT NOT NULL DEFAULT 'personal',
    -- soft-delete flag so data isn't lost
    archived        BOOLEAN NOT NULL DEFAULT FALSE
);

-- Spatial index (critical for bbox queries)
CREATE INDEX idx_markers_location ON markers USING GIST (location);
CREATE INDEX idx_markers_type ON markers (type);
CREATE INDEX idx_markers_archived ON markers (archived);

-- Optional: tracks (GPX routes walked)
CREATE TABLE tracks (
    id          SERIAL PRIMARY KEY,
    name        TEXT,
    path        GEOMETRY(LineString, 4326),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    gpx_source  TEXT  -- 'manual', 'garmin', 'strava', etc.
);

CREATE INDEX idx_tracks_path ON tracks USING GIST (path);

-- Optional: confirmations (someone validated an existing marker)
CREATE TABLE confirmations (
    id          SERIAL PRIMARY KEY,
    marker_id   INTEGER NOT NULL REFERENCES markers(id) ON DELETE CASCADE,
    note        TEXT,
    still_valid BOOLEAN NOT NULL DEFAULT TRUE,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_confirmations_marker ON confirmations (marker_id);

-- Marker comments / updates (freeform community notes)
CREATE TABLE comments (
    id          SERIAL PRIMARY KEY,
    marker_id   INTEGER NOT NULL REFERENCES markers(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_comments_marker ON comments (marker_id);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER markers_updated_at
    BEFORE UPDATE ON markers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
