"""
ARIA — Algorithmic Modules
===========================
Pure Python, no ML, no side effects.
All functions are read-only DB queries + deterministic computation.

Agents call these via MCP tools. The MCP server injects the DB connection.
Results feed the LLM only after computation is done — agents never compute,
they interpret.

Modules:
  zone       — Zone density, sister zone ranking, stress classification, dead zone map
  session    — Rider EPH, session health score, dead run cost, churn signal
  restaurant — Delay baseline, deviation scoring, active pickup listing, assignment scoring
"""

from .zone import (
    compute_zone_density,
    compute_sister_zones,
    compute_zone_stress,
    compute_dead_zone_map,
)
from .session import (
    compute_current_eph,
    compute_session_health_score,
    compute_dead_run_cost,
    compute_churn_signal,
)
from .restaurant import (
    compute_restaurant_baseline,
    compute_delay_deviation,
    get_active_pickups,
    score_assignment,
)

__all__ = [
    # Zone
    "compute_zone_density",
    "compute_sister_zones",
    "compute_zone_stress",
    "compute_dead_zone_map",
    # Session
    "compute_current_eph",
    "compute_session_health_score",
    "compute_dead_run_cost",
    "compute_churn_signal",
    # Restaurant + Order
    "compute_restaurant_baseline",
    "compute_delay_deviation",
    "get_active_pickups",
    "score_assignment",
]
