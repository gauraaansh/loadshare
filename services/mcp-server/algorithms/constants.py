"""
ARIA — Shared Encoding Constants
==================================
Single source of truth for all categorical encodings used across:
  - ML model training (train_model3_deadzone.py, train_model1_persona.py)
  - ML server inference (inference.py, schemas.py)
  - Algorithmic modules (restaurant.py score_assignment)
  - MCP server agents (dead_run.py feature assembly)

Encoding was fixed to match what the models were actually trained on.
Prior bug: schemas.py had 0=commercial,1=residential — training used 0=hub,1=commercial.
All downstream code now imports from here. Never hardcode these elsewhere.
"""

# ── Zone type → integer (matches train_model3_deadzone.py) ────
# hub=0, commercial=1, residential=2, peripheral=3
ZONE_TYPE_ENC: dict[str, int] = {
    "hub":         0,
    "commercial":  1,
    "residential": 2,
    "peripheral":  3,
}
ZONE_TYPE_DEC: dict[int, str] = {v: k for k, v in ZONE_TYPE_ENC.items()}

# Default when zone_type is unknown / not set in boundary_geojson
ZONE_TYPE_ENC_DEFAULT = 2   # residential — conservative mid-risk default

# ── City tier → integer ────────────────────────────────────────
# Matches Kaggle dataset City column encoding in train_model2
CITY_TIER_ENC: dict[str, int] = {
    "Metropolitan": 0,
    "Urban":        1,
    "Semi-Urban":   2,
}
CITY_TIER_ENC_DEFAULT = 0   # Metropolitan — all seeded zones are Bangalore / Metro

# ── Traffic density → integer (Model 2 feature) ───────────────
TRAFFIC_ENC: dict[str, int] = {
    "low":    0,
    "medium": 1,
    "high":   2,
    "jam":    3,
}

# ── Weather → integer (Model 2 feature) ───────────────────────
WEATHER_ENC: dict[str, int] = {
    "Clear":      0,
    "Cloudy":     1,
    "Fog":        2,
    "Sandstorms": 3,
    "Rain":       4,
    "Heavy_Rain": 5,
}
