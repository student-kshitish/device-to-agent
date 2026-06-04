"""
d2a/sense/verdict_engine.py — VerdictEngine: device state → health verdict.

Pure rule-based. No machine learning. Every threshold is a named constant with
a comment explaining what it means in physical terms.

A dumb agent can act correctly by reading verdict + advice alone — no ML needed.

Verdict levels (best → worst):
  comfort  → everything nominal; agent may proceed at full capacity.
  caution  → approaching limits; agent should throttle workload.
  strain   → heavy load; agent should reduce active work now.
  distress → critical condition; agent must release this resource immediately.
  fatigue  → battery critically low; prefer a plugged device for this task.

Note: distress and fatigue are distinct failure modes — a fully-loaded AC device
is distress; a lightly-loaded device at 5% battery is fatigue.

Priority evaluation order: distress > strain > fatigue > caution > comfort.
Temperature rising-trend is checked before instantaneous value so a fast-ramping
device is flagged before it crosses the distress threshold.
"""

from d2a.sense_types import VERDICT_LEVELS, ADVICE

# ── Temperature thresholds (normalized 0..1) ──────────────────────────────────
# These map back through Normalizer: 0.75 → 0.75*(90-20)+20 = 72.5°C (hot)
#                                     0.90 → 0.90*(90-20)+20 = 83°C (throttle zone)
TEMP_CAUTION:  float = 0.75   # above this → caution band
TEMP_DISTRESS: float = 0.90   # above this → critical; agent must back off

# ── CPU / GPU load thresholds (normalized 0..1) ───────────────────────────────
# Peak utilization across CPU and GPU (whichever is higher).
# LOAD_CAUTION: sustained high utilization; scheduling latency starts rising.
# LOAD_STRAIN:  near-saturated; thermal pressure increases; queue depth fills up.
LOAD_CAUTION: float = 0.70   # above this → caution
LOAD_STRAIN:  float = 0.85   # above this → strain (reduce_load)

# ── Memory pressure threshold (normalized 0..1) ───────────────────────────────
# used_percent above this triggers strain. At 0.95+ the kernel starts heavy
# swapping which kills performance more reliably than CPU saturation.
MEM_STRAIN: float = 0.85

# ── Battery fatigue threshold (normalized 0..1) ───────────────────────────────
# capacity_pct below this → fatigue.  0.15 = 15%.
# Most OS battery warnings fire at 20%, so we use 15% to avoid false positives
# when the user has already acknowledged the warning and chosen to continue.
BATTERY_FATIGUE: float = 0.15

# ── Rising-trend escalation delta ─────────────────────────────────────────────
# If temperature is in the CAUTION band AND the per-reading delta exceeds this,
# escalate immediately to distress rather than waiting for the value to cross
# TEMP_DISTRESS. 0.03 normalized ≈ 2.1°C per reading — a fast thermal ramp that
# predicts the device will exceed TEMP_DISTRESS within a few more readings.
TEMP_RISING_ESCALATION_DELTA: float = 0.03


def _get(features: dict, name: str, default: float = 0.0) -> float:
    """Look up a named feature from the flat vector. O(n) — vector is small."""
    try:
        idx = features["names"].index(name)
        return float(features["vector"][idx])
    except (ValueError, IndexError, KeyError, TypeError):
        return default


class VerdictEngine:
    """
    Maps current device state to a (verdict, advice) pair.
    Uses both instantaneous normalized values and trend data from the feature vector.
    """

    def judge(self, normalized: dict, features: dict) -> tuple[str, str]:
        """
        Evaluate device health and return (verdict, advice).
        verdict ∈ VERDICT_LEVELS; advice ∈ ADVICE (same index).
        """
        # ── Temperature (checks instantaneous + rising-trend) ──────────────────
        temp       = self._max_temp(normalized)
        temp_delta = _get(features, "thermal.max_temp_c.delta")

        if temp >= TEMP_DISTRESS:
            return "distress", "release_now"

        # Caution band + fast-rising → escalate to distress proactively
        if temp >= TEMP_CAUTION and temp_delta >= TEMP_RISING_ESCALATION_DELTA:
            return "distress", "release_now"

        # ── CPU + GPU peak load ────────────────────────────────────────────────
        cpu_util = normalized.get("cpu",  {}).get("util_pct", 0.0)
        gpu_util = normalized.get("gpu",  {}).get("util_pct", 0.0)
        cpu_util = float(cpu_util) if isinstance(cpu_util, (int, float)) else 0.0
        gpu_util = float(gpu_util) if isinstance(gpu_util, (int, float)) else 0.0
        peak_load = max(cpu_util, gpu_util)

        if peak_load >= LOAD_STRAIN:
            return "strain", "reduce_load"

        # ── Memory pressure ────────────────────────────────────────────────────
        mem_used = normalized.get("memory", {}).get("used_percent", 0.0)
        mem_used = float(mem_used) if isinstance(mem_used, (int, float)) else 0.0
        if mem_used >= MEM_STRAIN:
            return "strain", "reduce_load"

        # ── Battery fatigue ────────────────────────────────────────────────────
        battery_cap = self._battery_capacity(normalized)
        if battery_cap is not None and battery_cap <= BATTERY_FATIGUE:
            return "fatigue", "prefer_plugged_device"

        # ── Caution band: temperature or load approaching limits ───────────────
        if temp >= TEMP_CAUTION:
            return "caution", "throttle"

        if peak_load >= LOAD_CAUTION:
            return "caution", "throttle"

        # ── All nominal ────────────────────────────────────────────────────────
        return "comfort", "proceed"

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _max_temp(normalized: dict) -> float:
        """Highest normalized temperature across all thermal data, or 0.0."""
        thermal = normalized.get("thermal", {})
        if not isinstance(thermal, dict) or thermal.get("_unavailable"):
            return 0.0
        max_t = thermal.get("max_temp_c", 0.0)
        return float(max_t) if isinstance(max_t, (int, float)) else 0.0

    @staticmethod
    def _battery_capacity(normalized: dict) -> float | None:
        """Normalized battery capacity [0, 1], or None if no battery source."""
        battery = normalized.get("battery", {})
        if not isinstance(battery, dict) or battery.get("_unavailable"):
            return None
        cap = battery.get("capacity_pct")
        return float(cap) if isinstance(cap, (int, float)) else None
