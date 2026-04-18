"""
Pre-built heatmap points from the sample dataset.
Loaded once at startup to serve the map overlay quickly.
"""

import json
from pathlib import Path

_sample = Path(__file__).parent / "data" / "sample_events.json"

def _load():
    if not _sample.exists():
        return []
    with open(_sample) as f:
        events = json.load(f)
    return [
        [float(e["lat"]), float(e["lon"]), 0.8]
        for e in events
        if e.get("lat") and e.get("lon")
    ]

HEATMAP_POINTS = _load()
