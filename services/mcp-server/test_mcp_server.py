#!/usr/bin/env python3
"""
ARIA — MCP Server Smoke Tests
================================
Hits every public tool endpoint and the cycle trigger with realistic
expectations. Does NOT require the full agent cycle to have run — each
test gracefully handles empty-data responses (no cycle yet).

Usage:
    # Against local dev server
    python test_mcp_server.py

    # Against Docker container (from host)
    MCP_SERVER_URL=http://localhost:8001 python test_mcp_server.py

    # Against internal Docker network (from another container)
    MCP_SERVER_URL=http://aria-mcp-server:8001 python test_mcp_server.py
"""

import os
import sys
import json
import time
import httpx

BASE_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8001")
API_KEY  = os.getenv("MCP_API_KEY",    "aria_mcp_key_change_me")
HEADERS  = {"X-API-Key": API_KEY}

PASS = "\u2705 PASS"
FAIL = "\u274c FAIL"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    symbol = PASS if ok else FAIL
    print(f"  {symbol}  {name}" + (f" — {detail}" if detail else ""))


def get(path: str, params: dict | None = None, timeout: float = 10.0):
    url = f"{BASE_URL}{path}"
    r = httpx.get(url, headers=HEADERS, params=params or {}, timeout=timeout)
    return r


# ══════════════════════════════════════════════════════════════
# 1. Health
# ══════════════════════════════════════════════════════════════

print("\n── Health ──────────────────────────────────────────────────")
try:
    r = httpx.get(f"{BASE_URL}/health", timeout=5)
    check("GET /health returns 200",   r.status_code == 200)
    check("GET /health returns ok",    r.json().get("status") == "ok")
except Exception as e:
    check("GET /health", False, str(e))

# ══════════════════════════════════════════════════════════════
# 2. Auth guard
# ══════════════════════════════════════════════════════════════

print("\n── Auth ────────────────────────────────────────────────────")
try:
    r = httpx.get(f"{BASE_URL}/tools/cycle-briefing", timeout=5)
    check("No API key → 403",  r.status_code == 403, f"got {r.status_code}")
    r2 = httpx.get(f"{BASE_URL}/tools/cycle-briefing", headers={"X-API-Key": "wrong"}, timeout=5)
    check("Wrong API key → 403", r2.status_code == 403, f"got {r2.status_code}")
except Exception as e:
    check("Auth guard", False, str(e))

# ══════════════════════════════════════════════════════════════
# 3. Tool endpoints
# ══════════════════════════════════════════════════════════════

print("\n── Tool endpoints ──────────────────────────────────────────")

TOOLS: list[tuple[str, dict, list[str]]] = [
    # (path, params, required_response_keys)
    ("/tools/cycle-briefing",      {"n": 1},                   ["briefings"]),
    ("/tools/zone-intelligence",   {},                          []),
    ("/tools/zone-recommendations",{},                          []),
    ("/tools/zone-map",            {"type": "geometry"},        ["zones", "total", "last_updated"]),
    ("/tools/zone-map",            {"type": "stress"},          ["zones", "total", "last_updated"]),
    ("/tools/restaurant-risks",    {"limit": 5},                ["restaurants", "count"]),
    ("/tools/dead-run-risks",      {"limit": 5},                []),
    ("/tools/dead-zone-snapshots", {},                          []),
    ("/tools/rider-health",        {},                          []),
    ("/tools/rider-alerts",        {},                          []),
    ("/tools/churn-signals",       {},                          []),
    ("/tools/operator-alerts",     {},                          []),
    ("/tools/rider-interventions", {"limit": 5},                ["interventions", "count"]),
    ("/tools/bootstrap-status",    {},                          []),
    ("/tools/system-status",       {},                          ["healthy", "services"]),
]

for path, params, required_keys in TOOLS:
    label = path.replace("/tools/", "") + (f"?type={params['type']}" if "type" in params else "")
    try:
        r = get(path, params)
        ok_status = r.status_code == 200
        check(f"GET {label} → 200", ok_status, f"got {r.status_code}" if not ok_status else "")

        if ok_status and required_keys:
            body = r.json()
            for key in required_keys:
                check(f"  {label} has '{key}'", key in body, f"keys: {list(body.keys())[:6]}")
    except Exception as e:
        check(f"GET {label}", False, str(e))

# ══════════════════════════════════════════════════════════════
# 4. Zone map — encoding correctness
# ══════════════════════════════════════════════════════════════

print("\n── Zone map encoding ───────────────────────────────────────")
try:
    r = get("/tools/zone-map", {"type": "both"})
    if r.status_code == 200:
        zones = r.json().get("zones", [])
        if zones:
            valid_types = {"hub", "commercial", "residential", "peripheral", ""}
            bad = [z["zone_id"] for z in zones if z.get("zone_type") not in valid_types]
            check("All zone_types are canonical",  len(bad) == 0,
                  f"{len(bad)} zones with non-canonical type" if bad else "")
            valid_stress = {"dead", "low", "normal", "stressed", "stale", "unknown"}
            bad_stress = [z["zone_id"] for z in zones if z.get("stress_level") not in valid_stress]
            check("All stress_levels are valid",   len(bad_stress) == 0,
                  f"{len(bad_stress)} zones with invalid stress_level" if bad_stress else "")
        else:
            check("Zone map has zones (run seeder first)", False, "zones list empty")
    else:
        check("zone-map both mode → 200", False, f"got {r.status_code}")
except Exception as e:
    check("Zone map encoding", False, str(e))

# ══════════════════════════════════════════════════════════════
# 5. Cycle trigger
# ══════════════════════════════════════════════════════════════

print("\n── Manual cycle trigger ────────────────────────────────────")
try:
    r = httpx.post(f"{BASE_URL}/cycle/run", timeout=120)
    check("POST /cycle/run → 200",       r.status_code == 200, f"got {r.status_code}")
    if r.status_code == 200:
        body = r.json()
        check("Cycle returns severity_level",  "severity_level" in body)
        check("Cycle returns situation_summary","situation_summary" in body)
        check("Cycle returns patterns_detected","patterns_detected" in body)
        check("severity_level is valid",
              body.get("severity_level") in ("critical", "warning", "normal"),
              str(body.get("severity_level")))
except Exception as e:
    check("POST /cycle/run", False, str(e))

# ══════════════════════════════════════════════════════════════
# 6. Post-cycle: briefing is persisted
# ══════════════════════════════════════════════════════════════

print("\n── Post-cycle persistence ──────────────────────────────────")
try:
    r = get("/tools/cycle-briefing", {"n": 1})
    if r.status_code == 200:
        briefings = r.json().get("briefings", [])
        check("cycle-briefing returns ≥1 row",  len(briefings) >= 1, f"got {len(briefings)}")
        if briefings:
            row = briefings[0]
            check("briefing has cycle_id",        "cycle_id"       in row)
            check("briefing has timestamp",        "timestamp"      in row)
            check("briefing has severity_level",   "severity_level" in row)
            check("briefing.briefing is dict",
                  isinstance(row.get("briefing"), dict),
                  type(row.get("briefing")).__name__)
except Exception as e:
    check("Post-cycle persistence", False, str(e))

# ══════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════

print("\n" + "═" * 55)
total  = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = total - passed
print(f"  {passed}/{total} passed   ({failed} failed)")

if failed:
    print("\n  Failed tests:")
    for name, ok, detail in results:
        if not ok:
            print(f"    {FAIL}  {name}" + (f" — {detail}" if detail else ""))
    print()
    sys.exit(1)
else:
    print(f"\n  {PASS}  All tests passed\n")
    sys.exit(0)
