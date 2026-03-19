"""
ARIA — Event Stream: Simulation Clock
=======================================
Never call datetime.now() or asyncio.sleep() directly in the simulation.
Always go through SimClock so TIME_SCALE works correctly.

clock.now()          → current simulation datetime
clock.sleep(n)       → sleep n sim-seconds (real sleep = n / TIME_SCALE)
clock.hour()         → current sim hour of day (0-23)
clock.is_peak()      → True if current sim hour is a peak hour
clock.pause()        → freeze simulation time
clock.resume()       → unfreeze
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import PEAK_HOURS


class SimClock:
    def __init__(self, time_scale: float = 1.0, sim_start: Optional[datetime] = None):
        self._time_scale = time_scale
        self._real_start = datetime.now(timezone.utc)
        # sim_start defaults to real wall time — allows override for replays
        self._sim_start  = sim_start or self._real_start
        self._paused = False
        self._pause_real_at: Optional[datetime] = None
        self._total_pause_real_secs: float = 0.0

    # ── Time ──────────────────────────────────────────────────

    def now(self) -> datetime:
        """Return the current simulation datetime."""
        if self._paused and self._pause_real_at:
            real_elapsed = (self._pause_real_at - self._real_start).total_seconds()
        else:
            real_elapsed = (datetime.now(timezone.utc) - self._real_start).total_seconds()

        real_elapsed = max(0.0, real_elapsed - self._total_pause_real_secs)
        sim_elapsed  = real_elapsed * self._time_scale
        return self._sim_start + timedelta(seconds=sim_elapsed)

    async def sleep(self, sim_seconds: float) -> None:
        """Sleep for sim_seconds of simulation time."""
        real_secs = max(0.0, sim_seconds / self._time_scale)
        await asyncio.sleep(real_secs)

    # ── Convenience ───────────────────────────────────────────

    def hour(self) -> int:
        return self.now().hour

    def is_peak(self) -> bool:
        return self.hour() in PEAK_HOURS

    def sim_date(self):
        return self.now().date()

    # ── Pause / resume ────────────────────────────────────────

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            self._pause_real_at = datetime.now(timezone.utc)

    def resume(self) -> None:
        if self._paused and self._pause_real_at:
            self._total_pause_real_secs += (
                datetime.now(timezone.utc) - self._pause_real_at
            ).total_seconds()
            self._paused = False
            self._pause_real_at = None

    # ── Properties ────────────────────────────────────────────

    def set_time_scale(self, new_scale: float) -> None:
        """Change time_scale on the fly without a discontinuity in sim time."""
        current_sim = self.now()
        real_now    = datetime.now(timezone.utc)
        # Re-anchor so that now() still returns current_sim after the change
        self._real_start              = real_now
        self._sim_start               = current_sim
        self._total_pause_real_secs   = 0.0
        if self._paused:
            self._pause_real_at = real_now
        self._time_scale = new_scale

    @property
    def time_scale(self) -> float:
        return self._time_scale

    @property
    def is_paused(self) -> bool:
        return self._paused
