# AccessMap

A mobile-first web app for logging real-world outdoor accessibility friction points — gates, stiles, mud, steep sections — as spatial markers on a map.

---

## Quick start (Docker)

```bash
# 1. Install Docker Desktop if you haven't already
# 2. From the project root:
docker compose up --build
```

Then open:
- **App:** http://localhost:3000
- **API docs:** http://localhost:8000/docs

The database is initialised automatically on first run.

---

## Manual setup (without Docker)

### Prerequisites
- Python 3.12+
- PostgreSQL 16 with PostGIS extension

### Database
```bash
createdb accessmap
psql accessmap < db/init.sql
```

### Backend
```bash
cd backend
pip install -r requirements.txt
DATABASE_URL=postgresql://youruser:yourpassword@localhost:5432/accessmap \
  uvicorn main:app --reload --port 8000
```

### Frontend
Serve the `frontend/` directory with any static file server:
```bash
# Python built-in
python3 -m http.server 3000 --directory frontend
```

---

## Project structure

```
.
├── docker-compose.yml
├── db/
│   └── init.sql          # Schema: markers, tracks, confirmations
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py           # FastAPI app + lifespan
│   ├── db.py             # DB connection pool
│   ├── models/
│   │   └── marker.py     # Pydantic schemas
│   └── routers/
│       └── markers.py    # All marker endpoints
└── frontend/
    └── index.html        # Single-file Leaflet app
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/markers?min_lat=&min_lng=&max_lat=&max_lng=` | List markers in bounding box |
| POST | `/api/markers` | Create marker (multipart/form-data) |
| GET | `/api/markers/{id}` | Get single marker |
| DELETE | `/api/markers/{id}` | Soft-delete marker |
| POST | `/api/markers/{id}/confirm` | Confirm / invalidate marker |
| GET | `/health` | Health check |

---

## Using the app

1. **Locate yourself** — tap ⊕ (bottom-right) to jump to your GPS position
2. **Drop a marker** — tap **＋** to mark your current location, or long-press the map for a precise spot
3. **Pick type + severity** — choose from the 8 types, optionally rate difficulty and add a note or photo
4. **Save** — marker appears on the map immediately

---

## Decisions you'll want to make eventually

- **Auth** — currently open; add an API key header or OAuth before sharing with others
- **Photo storage** — uploads go into a Docker volume; swap for S3/R2 if deploying externally
- **Deployment** — works locally as-is; Fly.io / Railway are easy next steps for a hosted version
- **iOS GPS** — if you want background GPS tracking, a lightweight PWA manifest + service worker is the next step before going native
