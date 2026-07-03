CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TYPE marker_type AS ENUM (
    'gate',
    'kissing_gate',
    'stile',
    'steep',
    'mud',
    'narrow',
    'rough_surface',
    'field',
    'passage',
    'other'
);

CREATE TYPE severity_level AS ENUM (
    'low',
    'medium',
    'high'
);

CREATE TABLE profiles (
    id           TEXT PRIMARY KEY,
    email        TEXT,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE markers (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT,
    location        GEOMETRY(Point, 4326) NOT NULL,
    type            marker_type NOT NULL,
    subtype         TEXT,
    subtypes        JSONB NOT NULL DEFAULT '[]'::jsonb,
    severity        severity_level,
    note            TEXT,
    photo_url       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source          TEXT NOT NULL DEFAULT 'personal',
    archived        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_markers_location ON markers USING GIST (location);
CREATE INDEX idx_markers_type ON markers (type);
CREATE INDEX idx_markers_archived ON markers (archived);
CREATE INDEX idx_markers_user_id ON markers (user_id);

CREATE TABLE tracks (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT,
    name        TEXT,
    path        GEOMETRY(LineString, 4326),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    gpx_source  TEXT
);

CREATE INDEX idx_tracks_path ON tracks USING GIST (path);
CREATE INDEX idx_tracks_user_id ON tracks (user_id);

CREATE TABLE confirmations (
    id            SERIAL PRIMARY KEY,
    marker_id      INTEGER NOT NULL REFERENCES markers(id) ON DELETE CASCADE,
    user_id        TEXT,
    note           TEXT,
    still_valid    BOOLEAN NOT NULL DEFAULT TRUE,
    confirmed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_confirmations_marker ON confirmations (marker_id);

CREATE TABLE comments (
    id          SERIAL PRIMARY KEY,
    marker_id   INTEGER NOT NULL REFERENCES markers(id) ON DELETE CASCADE,
    user_id     TEXT,
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_comments_marker ON comments (marker_id);

CREATE TABLE strava_accounts (
    athlete_id     BIGINT PRIMARY KEY,
    user_id        TEXT UNIQUE,
    access_token   TEXT NOT NULL,
    refresh_token  TEXT NOT NULL,
    expires_at     BIGINT NOT NULL,
    firstname      TEXT,
    lastname       TEXT,
    connected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE strava_activities (
    activity_id   BIGINT PRIMARY KEY,
    user_id       TEXT,
    athlete_id    BIGINT,
    name          TEXT,
    sport_type    TEXT,
    distance_m    DOUBLE PRECISION,
    start_date    TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'pending',
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_strava_activities_user_status ON strava_activities (user_id, status);

CREATE TABLE strava_oauth_states (
    state       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE claimed_areas (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT,
    osm_type    TEXT,
    osm_id      BIGINT,
    name        TEXT,
    kind        TEXT,
    area        GEOMETRY(MultiPolygon, 4326) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_claimed_areas_geom ON claimed_areas USING GIST (area);
CREATE INDEX idx_claimed_areas_user ON claimed_areas (user_id);
CREATE UNIQUE INDEX idx_claimed_areas_user_osm
    ON claimed_areas (user_id, osm_type, osm_id) WHERE osm_id IS NOT NULL;

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
