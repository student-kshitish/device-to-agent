"""
d2a/sense/confidence_engine.py — ConfidenceEngine: signal quality → [0, 1].

An ML agent down-weights inference when sensory confidence is low.
A rule-based agent can show confidence alongside verdicts so users understand
when readings may be degraded.

Confidence is lowered by:
  - Sources that returned _unavailable (hardware absent or read failed)
  - Values outside physically impossible bounds (negative util, 200°C)
  - Missing expected key fields for a source

Confidence stays high when all sources return clean values within normal range.
"""

# ── Sanity bounds (ABSOLUTE, not normalized) ───────────────────────────────────
# These represent physically impossible readings — not merely extreme, but wrong.
# A reading outside these bounds almost certainly means sensor malfunction or
# a parsing error; confidence should be lowered even if the source "succeeded".

# Temperatures: below absolute zero is impossible; above 130°C = silicon already dead
TEMP_SANE_MIN_C: float =   0.0
TEMP_SANE_MAX_C: float = 130.0

# CPU utilization: negative is impossible; >100% is a parsing artifact
CPU_UTIL_SANE_MIN: float =   0.0
CPU_UTIL_SANE_MAX: float = 100.0

# Memory used percent: negative impossible; >100% is a parsing artifact
MEM_USED_SANE_MIN: float =   0.0
MEM_USED_SANE_MAX: float = 100.0

# Battery capacity percent: 0 = empty (reportable), >100% is impossible
BATTERY_CAP_SANE_MIN: float =   0.0
BATTERY_CAP_SANE_MAX: float = 100.0

# ── Penalty weights ───────────────────────────────────────────────────────────
# Each unavailable source deducts UNAVAILABLE_PENALTY from 1.0.
# Each insane value deducts INSANE_VALUE_PENALTY.
# Values are chosen so a single bad source still leaves >0.5 confidence,
# but two bad sources pull confidence below 0.6.
UNAVAILABLE_PENALTY:  float = 0.20
INSANE_VALUE_PENALTY: float = 0.10


class ConfidenceEngine:
    """
    Computes a scalar confidence ∈ [0, 1] for a single sense pipeline run.

    Inputs:
      raw:        {source_name: dict} from RawCollector  — used for absolute sanity checks
      normalized: {source_name: dict} from Normalizer    — used for structural checks
    """

    def score(self, raw: dict, normalized: dict) -> float:
        """Return confidence in [0, 1]. 0.0 if no sources at all."""
        if not raw:
            return 0.0

        penalty = 0.0

        for source_name, source_data in raw.items():
            # ── unavailable source (hardware missing or read() raised) ─────────
            if isinstance(source_data, dict) and source_data.get("_unavailable"):
                penalty += UNAVAILABLE_PENALTY
                continue

            # ── per-source sanity checks on raw (absolute) values ─────────────
            if not isinstance(source_data, dict):
                continue

            if source_name == "thermal":
                max_t = source_data.get("max_temp_c")
                if isinstance(max_t, (int, float)):
                    if not (TEMP_SANE_MIN_C <= max_t <= TEMP_SANE_MAX_C):
                        penalty += INSANE_VALUE_PENALTY
                # also check the array of zone temps
                for t in source_data.get("temps_c", []):
                    if isinstance(t, (int, float)):
                        if not (TEMP_SANE_MIN_C <= t <= TEMP_SANE_MAX_C):
                            penalty += INSANE_VALUE_PENALTY
                            break  # one insane temp is enough to penalise once

            elif source_name == "cpu":
                util = source_data.get("util_pct")
                if isinstance(util, (int, float)):
                    if not (CPU_UTIL_SANE_MIN <= util <= CPU_UTIL_SANE_MAX):
                        penalty += INSANE_VALUE_PENALTY

            elif source_name == "memory":
                used = source_data.get("used_percent")
                if isinstance(used, (int, float)):
                    if not (MEM_USED_SANE_MIN <= used <= MEM_USED_SANE_MAX):
                        penalty += INSANE_VALUE_PENALTY

            elif source_name == "battery":
                cap = source_data.get("capacity_pct")
                if isinstance(cap, (int, float)):
                    if not (BATTERY_CAP_SANE_MIN <= cap <= BATTERY_CAP_SANE_MAX):
                        penalty += INSANE_VALUE_PENALTY

        return round(max(0.0, 1.0 - penalty), 4)
