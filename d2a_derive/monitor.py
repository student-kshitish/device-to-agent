"""
d2a_derive/monitor.py — per-input staleness monitor for a live DerivedCapability.

A lease can be perfectly alive while frames quietly stop flowing — a stalled
source, a wedged stream, a device that has gone quiet without announcing anything.
That is not a lease loss (the healer never fires), so it needs its own detector:

    no frame on an input within  staleness_factor × its expected interval
        → that input goes "degraded" (reason: staleness)
    a fresh frame on a stale input
        → it flips back to "active" (recovery, handled in executor._ingest)

The monitor only ever DEGRADES a currently-active input; it never touches an input
the healer is rebinding or has marked gone (those states are the healer's), and it
never busy-spins — it wakes on a stop-aware interval derived from the fastest
input's cadence. Overall DerivedCapability state (and on_state_change) follows from
the per-input states through the executor's one state-folding path.

health() is served by DerivedCapability itself (it owns the per-input counters);
the monitor's whole job is flipping the staleness bit on time.
"""

import threading
import time

from d2a_derive import executor as _ex


class StalenessMonitor:
    def __init__(self, dc, *, staleness_factor: float = 3.0,
                 interval_s: float | None = None):
        self.dc = dc
        self.staleness_factor = max(1.0, float(staleness_factor))
        self.interval_s = interval_s if interval_s is not None else self._auto_interval()
        self._stop = threading.Event()
        self._thread = None

    def _auto_interval(self) -> float:
        """Poll about twice as fast as the fastest input's expected interval, so a
        staleness threshold of N intervals is caught promptly. Clamped to a sane
        band so a slow recipe doesn't leave us checking once a minute."""
        fastest = min((f.min_interval_s for f in self.dc._feeds), default=0.5)
        return min(max(fastest / 2.0, 0.05), 0.5)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"derive-monitor-{self.dc.provided_name}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._check_once()
            except Exception:
                pass

    def _check_once(self) -> None:
        dc = self.dc
        now = time.time()
        changed = False
        with dc._lock:
            if dc._closed:
                return
            # Phase 6: sample the worst current input staleness for the recipe's
            # lifetime metrics. In-memory only (dc folds it into this run's mean);
            # bounded to the monitor's own cadence, never a per-frame write.
            samples = [now - f.last_frame_ts for f in dc._feeds
                       if f.last_frame_ts and not f.is_inner]
            if samples:
                dc._note_staleness_locked(max(samples))
            for feed in dc._feeds:
                # only an ACTIVE input can go stale here; rebinding/gone belong to
                # the healer, and a not-yet-fed input has no baseline to judge. A
                # CHAINED (inner) feed's health is owned by the inner derivation's
                # own monitor + the outward mirror — skip it here.
                if feed.is_inner or feed.state != _ex.IN_ACTIVE or not feed.last_frame_ts:
                    continue
                threshold = self.staleness_factor * feed.min_interval_s
                if now - feed.last_frame_ts > threshold:
                    feed.state = _ex.IN_DEGRADED
                    changed = True
                    print(f"[monitor:{dc.provided_name}] input '{feed.hint}' "
                          f"stale ({now - feed.last_frame_ts:.2f}s > "
                          f"{threshold:.2f}s) → degraded")
            if changed:
                dc._recompute_state_locked("staleness")
