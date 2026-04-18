"""
ACLED data client.
ACLED (Armed Conflict Location & Event Data Project) provides free conflict event data.
Registration: https://developer.acleddata.com/

Falls back to bundled sample data automatically if no API key is configured.
"""

import json
import os
import time
import sqlite3
import urllib.request
import urllib.parse
from pathlib import Path

ACLED_API_BASE = "https://api.acleddata.com/acled/read"
CACHE_DB = Path(__file__).parent / "data" / "events_cache.db"
SAMPLE_DATA = Path(__file__).parent / "data" / "sample_events.json"

# Cache events for 6 hours to avoid hammering the API
CACHE_TTL_SECONDS = 6 * 3600


def _init_cache():
    CACHE_DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            region_key TEXT PRIMARY KEY,
            fetched_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _cache_key(lat: float, lon: float, radius_km: float) -> str:
    # Round coords to ~1 degree grid for cache bucketing
    return f"{round(lat)},{round(lon)},{int(radius_km)}"


def get_events(
    lat: float,
    lon: float,
    radius_km: float = 150,
    days_back: int = 180,
    email: str = "",
    api_key: str = "",
) -> list:
    """
    Return conflict events near (lat, lon) within radius_km, over the last days_back days.
    Uses ACLED API if credentials provided, else falls back to sample data.
    Results are cached locally.
    """
    email = email or os.environ.get("ACLED_EMAIL", "")
    api_key = api_key or os.environ.get("ACLED_KEY", "")

    if email and api_key:
        return _fetch_acled(lat, lon, radius_km, days_back, email, api_key)
    else:
        return _load_sample(lat, lon, radius_km)


def _fetch_acled(lat, lon, radius_km, days_back, email, api_key) -> list:
    conn = _init_cache()
    key = _cache_key(lat, lon, radius_km)

    # Check cache freshness
    row = conn.execute(
        "SELECT fetched_at FROM fetch_log WHERE region_key = ?", (key,)
    ).fetchone()
    if row and (time.time() - row[0]) < CACHE_TTL_SECONDS:
        rows = conn.execute(
            "SELECT data FROM events WHERE id LIKE ?", (f"{key}_%",)
        ).fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    # Fetch from ACLED
    from datetime import datetime, timedelta
    start_date = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # ACLED uses bounding box; approximate from center + radius
    deg_lat = radius_km / 111.0
    deg_lon = radius_km / (111.0 * abs(math.cos(math.radians(lat))) + 0.001)

    params = {
        "key": api_key,
        "email": email,
        "event_date": start_date,
        "event_date_where": ">=",
        "latitude": f"{lat - deg_lat}|{lat + deg_lat}",
        "latitude_where": "BETWEEN",
        "longitude": f"{lon - deg_lon}|{lon + deg_lon}",
        "longitude_where": "BETWEEN",
        "fields": "event_id_cnty|event_date|event_type|sub_event_type|location|latitude|longitude|fatalities",
        "limit": 500,
    }

    url = ACLED_API_BASE + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"[ACLED] API fetch failed: {e} — falling back to sample data")
        conn.close()
        return _load_sample(lat, lon, radius_km)

    events = []
    for ev in data.get("data", []):
        normalized = {
            "id": ev.get("event_id_cnty", ""),
            "event_date": ev.get("event_date", ""),
            "event_type": ev.get("event_type", ""),
            "sub_event_type": ev.get("sub_event_type", ""),
            "location": ev.get("location", ""),
            "lat": ev.get("latitude", 0),
            "lon": ev.get("longitude", 0),
            "fatalities": ev.get("fatalities", 0),
            "source": "acled",
        }
        events.append(normalized)
        conn.execute(
            "INSERT OR REPLACE INTO events (id, data, fetched_at) VALUES (?, ?, ?)",
            (f"{key}_{normalized['id']}", json.dumps(normalized), time.time()),
        )

    conn.execute(
        "INSERT OR REPLACE INTO fetch_log (region_key, fetched_at) VALUES (?, ?)",
        (key, time.time()),
    )
    conn.commit()
    conn.close()
    return events


def _load_sample(lat: float, lon: float, radius_km: float) -> list:
    """Load bundled sample events, filtered roughly to the requested region."""
    if not SAMPLE_DATA.exists():
        return []
    with open(SAMPLE_DATA) as f:
        all_events = json.load(f)
    # Quick bbox filter before haversine in scorer
    deg = radius_km / 100  # rough buffer
    return [
        e for e in all_events
        if abs(float(e.get("lat", 0)) - lat) < deg + 5
        and abs(float(e.get("lon", 0)) - lon) < deg + 5
    ]


# needed for _fetch_acled bbox calc
import math
