"""
ARIA — Event Stream: Configuration
====================================
All constants and env-var-driven settings in one place.
Import from here everywhere — no os.getenv() scattered across files.
"""

import os

# ── Service ────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://aria:aria_secret@localhost:5432/aria_db")
REDIS_URL    = os.getenv("REDIS_URL",    "redis://localhost:6379/0")
PORT         = int(os.getenv("EVENT_STREAM_PORT", "8003"))

# ── Simulation clock ───────────────────────────────────────────
# TIME_SCALE=10 → 1 real second = 10 sim seconds (6min real = 1 sim hour)
# TIME_SCALE=1  → real-time (production / real rider mode)
TIME_SCALE = float(os.getenv("TIME_SCALE", "10.0"))

# ── Rider concurrency caps ─────────────────────────────────────
SIMULATION_PEAK_RIDERS    = int(os.getenv("SIMULATION_PEAK_RIDERS",    "150"))
SIMULATION_OFFPEAK_RIDERS = int(os.getenv("SIMULATION_OFFPEAK_RIDERS",  "40"))

# ── Shift length ranges (sim hours) ───────────────────────────
SUPPLEMENTARY_SHIFT_HOURS = (3.0,  5.0)
DEDICATED_SHIFT_HOURS     = (8.0, 12.0)

# ── Global cycle cadence ──────────────────────────────────────
# Controls BOTH zone snapshot interval (event-stream) AND supervisor cycle
# (MCP server). Single knob — change here, both services update.
# Default 15 min. Set to 5 for faster demos.
CYCLE_INTERVAL_MINS = int(os.getenv("CYCLE_INTERVAL_MINS", "15"))

# ── Loop tick intervals (real seconds, not sim-scaled) ────────
# Pipeline and dispatcher run on real-time ticks — they compare sim timestamps
PIPELINE_TICK_SECS   = 2    # order status advancement check
DISPATCHER_TICK_SECS = 5    # idle rider → assign order
SCHEDULER_TICK_SECS  = 30   # rider online/offline schedule

# ── Peak hours (24h, sim time) ────────────────────────────────
PEAK_HOURS = frozenset(range(7, 10)) | frozenset(range(12, 14)) | frozenset(range(18, 23))

# ── Travel speed by (zone_type, period) km/h ─────────────────
AVG_SPEEDS: dict[tuple[str, str], float] = {
    ("hub",         "peak"):    14.0,
    ("hub",         "offpeak"): 22.0,
    ("commercial",  "peak"):    12.0,
    ("commercial",  "offpeak"): 20.0,
    ("residential", "peak"):    18.0,
    ("residential", "offpeak"): 25.0,
    ("peripheral",  "peak"):    28.0,
    ("peripheral",  "offpeak"): 35.0,
}

# ── Fare calculation (INR) ─────────────────────────────────────
# Tuned so 3.3 orders/hr × avg Rs.27/order ≈ Rs.89/hr EPH (near Rs.90 target)
BASE_FARE_RS   = 15.0
PER_KM_RATE_RS =  4.0
LD_BONUS_RS    = 15.0   # long-distance (> 5km) bonus

# ── Dead zone / risk thresholds ───────────────────────────────
DEAD_ZONE_STRESS_THRESHOLD = float(os.getenv("DEAD_ZONE_STRESS_THRESHOLD", "0.5"))

# ── Restaurant queue model ─────────────────────────────────────
# prep_capacity derived from avg_prep_time_mins / PREP_TIME_PER_SLOT
PREP_TIME_PER_SLOT = 5.0   # minutes per capacity slot
