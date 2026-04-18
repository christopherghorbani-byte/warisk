"""
WarRisk — AI-powered war-risk parametric insurance platform
Run: python app.py
"""

import os
import json
from flask import Flask, render_template, request, jsonify
from scorer import score_location
from acled import get_events

app = Flask(__name__)

ACLED_EMAIL = os.environ.get("ACLED_EMAIL", "")
ACLED_KEY   = os.environ.get("ACLED_KEY", "")


@app.route("/")
def index():
    has_live_data = bool(ACLED_EMAIL and ACLED_KEY)
    return render_template("index.html", has_live_data=has_live_data)


@app.route("/api/score", methods=["POST"])
def api_score():
    body = request.get_json(force=True)
    lat           = float(body.get("lat", 0))
    lon           = float(body.get("lon", 0))
    asset_type    = str(body.get("asset_type", "warehouse"))
    insured_value = float(body.get("insured_value", 1_000_000))

    events = get_events(
        lat=lat, lon=lon,
        radius_km=150, days_back=180,
        email=ACLED_EMAIL, api_key=ACLED_KEY,
    )

    result = score_location(lat, lon, asset_type, insured_value, events)
    annual_premium_usd = round(insured_value * result.annual_premium_pct / 100, 0)

    return jsonify({
        "risk_score":         result.risk_score,
        "risk_label":         result.risk_label,
        "event_count":        result.event_count,
        "closest_event_km":   result.closest_event_km,
        "annual_premium_pct": result.annual_premium_pct,
        "annual_premium_usd": annual_premium_usd,
        "parametric_trigger": result.parametric_trigger,
        "factors":            result.factors,
        "nearby_events":      result.nearby_events,
        "data_source":        "ACLED Live" if (ACLED_EMAIL and ACLED_KEY) else "Sample Data",
    })


@app.route("/api/heatmap", methods=["GET"])
def api_heatmap():
    """Return conflict event points for the map heatmap layer."""
    from data_index import HEATMAP_POINTS
    return jsonify(HEATMAP_POINTS)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)
