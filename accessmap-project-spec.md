# AccessMap — Project Specification

> A mobile-first, map-based system for logging and visualising real-world outdoor accessibility friction points (e.g. gates, stiles, terrain difficulty), enabling users to build a persistent spatial memory layer of navigational barriers and conditions for personal route planning.

---

## 1. Core Idea

A personal (and eventually shared) accessibility annotation system for outdoor spaces — woods, fields, trails, countryside paths.

A map where users can:

- Walk/run/roll through real environments
- Drop simple location-based markers when they encounter mobility-relevant obstacles:
  - Gates (e.g. kissing gates)
  - Stiles
  - Steep sections
  - Mud / rough terrain
  - Narrow sections
  - Other barriers
- Attach notes ("passable but difficult"), photos (current condition), optional severity or context
- Later revisit the map to plan outings, avoid known barriers, and understand terrain friction points

### Key Philosophy

Not *"is this accessible?"* but:

> **"Where exactly are the friction points in real terrain, and what are they like?"**

A **digital pen-on-paper map** — freeform spatial annotation built from lived experience, gradually enriched over time.

---

## 2. Intended User Experience

### Primary initial use case
You and your mum walking in local woods/countryside.

### Workflow
1. Walk a route (GPS optional initially)
2. Encounter something relevant (e.g. stile, muddy section)
3. Drop a marker instantly on phone: type (gate / steep / mud / narrow) + optional note or photo
4. Later: view map of known obstacles, decide where to walk safely/comfortably

### Later extension (optional)
- Friends contribute while running/walking
- "Confirmation" of existing markers ("yes, still rough")
- Gradual crowd-sourced refinement

---

## 3. Technical Approach

### 3.1 MVP Architecture

**Backend**
- Python (FastAPI)
- PostgreSQL + PostGIS extension

**Core data model — `points` table**

| Field | Type | Notes |
|-------|------|-------|
| `id` | serial | Primary key |
| `latitude` / `longitude` | float | PostGIS geometry |
| `timestamp` | timestamptz | Created at |
| `type` | enum | gate, stile, mud, steep, narrow, rough, other |
| `severity` | enum | low / medium / high |
| `note` | text | Optional free text |
| `photo_url` | text | Optional |
| `source` | text | personal / confirmed |
| `track_id` | int | Optional FK to tracks table |

**Optional tables**
- `tracks` — GPX uploads / routes walked
- `confirmations` — multi-user agreement layer

### 3.2 Frontend

**MVP (recommended)**
- Mobile-first web app
- Leaflet.js + OpenStreetMap
- Features: map view, "drop marker here" button, GPS current location, view/edit markers

**Later**
- Native mobile app (React Native / Flutter) — only needed for background GPS tracking or polish

### 3.3 GPS Handling

The system does **not** require continuous tracking. It needs "current location at time of marker creation" only.

- Web GPS via browser geolocation API is sufficient for MVP
- Limitation: weaker background tracking, especially on iOS
- Native app needed only if background GPS becomes a requirement

### 3.4 Optional Future Enhancements
- GPX import (Garmin / Strava exports)
- Route replay + annotation along path
- Inferred "slowdown zones" from GPS drift patterns

---

## 4. Comparison with Existing Systems

### Hiking / Trail Apps

**AllTrails**
- Strengths: trail discovery, photos + reviews
- Gap: no structured obstacle layer; not map-first friction annotation

**Komoot**
- Strengths: route planning, elevation + surface data
- Gap: no persistent "this exact gate is problematic" layer; not accessibility-first

### Accessibility Mapping Tools

**Wheelmap**
- Strengths: wheelchair accessibility of venues
- Gap: does not model outdoor terrain/paths in detail; binary accessibility classification

### Trail Guide Systems

**FarOut**
- Strengths: curated long-distance trails, user notes layered on top
- Gap: only works for predefined routes; not open-ended local landscape annotation

### Mapping Infrastructure

**OpenStreetMap**
- Strengths: supports all relevant features (gates, stiles, surfaces); extremely powerful spatial data model
- Gap: not user-friendly for casual annotation; not designed as personal memory layer; lacks simple UX for real-time logging

### Current Informal Solution
Memory, photos, WhatsApp messages, hiking forum advice, Google Maps saved places.
Gap: not spatially structured, not persistent in map form, not searchable as "friction points".

---

## 5. Conceptual Positioning

This system is best described as:

> **A personal accessibility friction layer over real-world geography**

Not a route planner, not a hiking app, not a navigation tool — but a **spatial memory system for mobility-relevant obstacles**.

---

## 6. What Is Novel

Not the data type (already exists in fragments), but the combination:

- Freeform map annotation
- Accessibility-specific tagging
- Personal + incremental data accumulation
- Optional multi-user validation
- Focus on **friction points** rather than routes or ratings

> Moving from "trail reviews" → "precise spatial obstacle memory"

---

## 7. Key Risks & Limitations

| Risk | Notes |
|------|-------|
| Existing overlap | Data exists in OSM and hiking apps; innovation is UX + focus, not new data |
| Data sparsity | Early usage feels empty; usefulness grows with repeated use |
| Subjectivity | "Rough", "steep", "narrow" vary by user ability, device, season |
| Contribution friction | **Critical:** marking must be ≤2 taps or users won't consistently log |
| GPS accuracy | ~5–15m drift in wooded areas; can misplace precise obstacles |
| Motivation decay | Passion project risk — value depends on sustained usage |
| Crowdsourcing | Limited natural incentive for strangers to contribute structured data |
| Offline capability | Wooded areas have poor signal; markers must queue offline and sync later |
| Temporal decay | "Mud" in January ≠ "mud" in July; markers need timestamps and aging |

---

## 8. Overall Evaluation

| Dimension | Rating |
|-----------|--------|
| Technical feasibility | ✅ Feasible with chosen stack |
| Stack fit (PostGIS) | ✅ Well suited |
| Web-first viability | ✅ Viable for MVP |
| Market uniqueness | ⚠️ Fragmented but not underserved mass market |
| Personal utility | ✅✅ High — directly aligned with lived experience |

**Passion project rating: 8 / 10**

High personal utility, technically achievable, clear problem statement, strong alignment with real-world need. Not higher because it's not fundamentally unique in the data space and relies on consistent personal use.

---

## 9. Design Principles

1. **3-tap drop** — dropping a marker must take ≤3 taps; everything else is secondary
2. **Offline-first** — store markers locally, sync when signal returns (IndexedDB queue)
3. **Temporal markers** — all markers carry a timestamp; fade/prompt re-confirmation after ~3 months
4. **Personal before social** — build for one user first; community layer is optional
5. **OSM compatibility** — use OSM tag schema (`barrier=stile`, `gate=kissing_gate`, `surface=mud`) to allow future export
