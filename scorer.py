"""
WarRisk scoring engine.
Rule-based + statistical. No ML training required — fast, explainable, runs on any machine.
Scoring factors: conflict event density, recency weighting, proximity to hotspot, asset type.
"""

import math
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

ASSET_MULTIPLIERS = {
    "warehouse":       1.10,
    "factory":         1.20,
    "infrastructure":  1.50,
    "hospital":        1.40,
    "residential":     0.85,
    "office":          1.00,
    "port":            1.60,
    "vessel":          1.70,
    "cargo":           1.30,
}

# ACLED event type severity weights
EVENT_SEVERITY = {
    "Shelling/artillery/missile attack": 10,
    "Air/drone strike":                  10,
    "Remote explosive/landmine/IED":     8,
    "Suicide bomb":                      8,
    "Attack":                            6,
    "Armed clash":                       5,
    "Looting/property destruction":      3,
    "Protest":                           1,
    "Riot":                              2,
    "Violence against civilians":        4,
}
DEFAULT_SEVERITY = 4

SEARCH_RADIUS_KM = 75.0
MAX_DENSITY_SCORE = 50.0
MAX_PROXIMITY_SCORE = 30.0
FRONT_LINE_SCORE = 20.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def recency_weight(event_date: str, today: Optional[datetime] = None) -> float:
    """Events in the last 30 days weight 1.0; decay exponentially to 0.1 at 180 days."""
    if today is None:
        today = datetime.utcnow()
    try:
        d = datetime.strptime(event_date[:10], "%Y-%m-%d")
    except Exception:
        return 0.5
    age_days = max(0, (today - d).days)
    if age_days <= 30:
        return 1.0
    if age_days > 180:
        return 0.1
    return max(0.1, math.exp(-0.015 * (age_days - 30)))


@dataclass
class ScoreResult:
    risk_score: float            # 0–100
    risk_label: str              # Low / Moderate / High / Severe
    event_count: int             # events in radius
    weighted_density: float      # recency/severity weighted density
    closest_event_km: float      # nearest event distance
    annual_premium_pct: float    # % of insured value per year
    parametric_trigger: str      # plain-language trigger description
    factors: dict                # breakdown for display
    nearby_events: list          # top 5 recent events for the UI


def score_location(
    lat: float,
    lon: float,
    asset_type: str,
    insured_value_usd: float,
    events: list,               # list of dicts from ACLED or sample data
) -> ScoreResult:
    """
    Core scoring function.
    events: each dict must have keys: lat, lon, event_date, event_type, location
    """
    asset_type = asset_type.lower()
    asset_mult = ASSET_MULTIPLIERS.get(asset_type, 1.0)

    today = datetime.utcnow()
    cutoff = today - timedelta(days=180)

    # Filter to relevant events
    nearby = []
    for ev in events:
        try:
            ev_lat = float(ev["lat"])
            ev_lon = float(ev["lon"])
        except (KeyError, ValueError, TypeError):
            continue
        dist = haversine(lat, lon, ev_lat, ev_lon)
        if dist > SEARCH_RADIUS_KM:
            continue
        try:
            ev_date = datetime.strptime(str(ev.get("event_date", ""))[:10], "%Y-%m-%d")
        except Exception:
            ev_date = cutoff
        if ev_date < cutoff:
            continue
        severity = EVENT_SEVERITY.get(ev.get("event_type", ""), DEFAULT_SEVERITY)
        weight = recency_weight(str(ev.get("event_date", "")), today) * severity
        nearby.append({**ev, "_dist_km": round(dist, 1), "_weight": weight})

    nearby.sort(key=lambda x: x["_dist_km"])

    # --- Density score (0–50) ---
    total_weight = sum(e["_weight"] for e in nearby)
    # normalize: 100 weighted-points = max score
    density_score = min(MAX_DENSITY_SCORE, (total_weight / 100) * MAX_DENSITY_SCORE)

    # --- Proximity score (0–30) ---
    if nearby:
        closest_km = nearby[0]["_dist_km"]
        proximity_score = MAX_PROXIMITY_SCORE * math.exp(-closest_km / 20)
    else:
        closest_km = SEARCH_RADIUS_KM
        proximity_score = 0.0

    # --- Regional baseline (0–20) ---
    # Simple heuristic: known high-tension zones get a floor
    regional_score = _regional_baseline(lat, lon)

    raw_score = density_score + proximity_score + regional_score
    final_score = min(100.0, raw_score * asset_mult)

    # --- Risk label ---
    if final_score >= 75:
        label = "Severe"
    elif final_score >= 50:
        label = "High"
    elif final_score >= 25:
        label = "Moderate"
    else:
        label = "Low"

    # --- Premium (annualized % of insured value) ---
    # Calibrated loosely against known market rates:
    # Severe: ~3–6% / year  |  High: 1–3%  |  Moderate: 0.3–1%  |  Low: 0.05–0.3%
    premium_pct = _premium_from_score(final_score)

    # --- Parametric trigger ---
    trigger = _parametric_trigger(final_score, asset_type)

    top_events = [
        {
            "location": e.get("location", "Unknown"),
            "event_type": e.get("event_type", "Unknown"),
            "event_date": str(e.get("event_date", ""))[:10],
            "dist_km": e["_dist_km"],
        }
        for e in nearby[:5]
    ]

    return ScoreResult(
        risk_score=round(final_score, 1),
        risk_label=label,
        event_count=len(nearby),
        weighted_density=round(total_weight, 1),
        closest_event_km=round(closest_km, 1),
        annual_premium_pct=round(premium_pct, 3),
        parametric_trigger=trigger,
        factors={
            "density_score": round(density_score, 1),
            "proximity_score": round(proximity_score, 1),
            "regional_baseline": round(regional_score, 1),
            "asset_multiplier": asset_mult,
        },
        nearby_events=top_events,
    )


def _regional_baseline(lat: float, lon: float) -> float:
    """
    Floors for regions with known persistent conflict risk even in quiet periods.
    Returns 0–20.
    """
    zones = [
        # (center_lat, center_lon, radius_km, baseline_score, label)
        (48.5,  33.5,  600, 15.0, "Ukraine"),
        (32.0,  35.0,  300, 14.0, "Levant"),
        (31.5,  34.5,  100, 16.0, "Gaza"),
        (15.5,  32.5,  400, 12.0, "Sudan"),
        (15.0,  42.5,  300, 10.0, "Yemen"),
        (26.5,  56.0,  200, 11.0, "Gulf of Hormuz"),
        (43.0,  41.5,  250, 10.0, "South Caucasus"),
        (13.0,  14.0,  400,  9.0, "Lake Chad Basin"),
        (6.0,    1.5,  300,  8.0, "West Africa"),
        (33.5,  44.0,  400,  9.0, "Iraq/Syria"),
    ]
    best = 0.0
    for clat, clon, radius, score, _ in zones:
        d = haversine(lat, lon, clat, clon)
        if d < radius:
            # Scale: full score at center, taper to 20% at edge
            contribution = score * (1 - 0.8 * (d / radius))
            best = max(best, contribution)
    return best


def _premium_from_score(score: float) -> float:
    """Map risk score to annualized premium % of insured value."""
    if score >= 80:
        return 3.0 + (score - 80) * 0.15   # 3–4.5%
    elif score >= 60:
        return 1.5 + (score - 60) * 0.075  # 1.5–3%
    elif score >= 40:
        return 0.5 + (score - 40) * 0.05   # 0.5–1.5%
    elif score >= 20:
        return 0.1 + (score - 20) * 0.02   # 0.1–0.5%
    else:
        return max(0.05, score * 0.005)


def _parametric_trigger(score: float, asset_type: str) -> str:
    if asset_type in ("vessel", "cargo"):
        if score >= 60:
            return "Automatic payout if vessel enters designated high-risk maritime corridor OR AIS signal lost for >48h in covered zone — no loss assessment required."
        return "Payout triggered if vessel sustains verified strike or seizure within policy zone, confirmed by two independent AIS/satellite sources within 72h."

    damage_threshold = 15 if score >= 70 else 25
    return (
        f"Automatic payout if satellite imagery (Sentinel-2 / Planet) confirms ≥{damage_threshold}% "
        f"structural damage to insured asset within covered grid cell — assessed within 5 business days, "
        f"no in-person adjuster required. Payout processed in ≤10 business days of trigger confirmation."
    )
