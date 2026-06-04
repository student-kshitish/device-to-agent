from __future__ import annotations
from d2a.contracts import IOContract

# ── scoring weights ────────────────────────────────────────────────────────────
W_HEALTH_TEMP    = 0.25   # low temperature = healthier
W_HEALTH_LOAD    = 0.25   # low cpu/memory load = healthier
W_HEALTH_BATTERY = 0.15   # battery present + level ok
W_RATE           = 0.20   # higher data rate relative to requirement
W_CONFIDENCE     = 0.15   # explicit confidence field if present


class CapabilityScorer:
    def score(self, candidate: dict, role_spec: dict) -> float:
        """
        Score a single candidate 0..1 using named, weighted factors.
        Higher = better.
        """
        live = candidate.get("live_state", {})
        contract = candidate.get("contract")

        health_temp = _score_temp(live)
        health_load = _score_load(live)
        health_batt = _score_battery(live)

        rate_score = _score_rate(contract, role_spec)
        conf_score = float(live.get("confidence", 1.0))
        conf_score = max(0.0, min(1.0, conf_score))

        total = (
            W_HEALTH_TEMP    * health_temp +
            W_HEALTH_LOAD    * health_load +
            W_HEALTH_BATTERY * health_batt +
            W_RATE           * rate_score  +
            W_CONFIDENCE     * conf_score
        )
        return round(total, 4)

    def rank(self, candidates: list[dict], role_spec: dict) -> list[dict]:
        """Return candidates sorted best-first with their scores attached."""
        scored = []
        for c in candidates:
            s = self.score(c, role_spec)
            entry = dict(c)
            entry["_score"] = s
            scored.append(entry)
        scored.sort(key=lambda x: x["_score"], reverse=True)
        return scored


# ── factor helpers ─────────────────────────────────────────────────────────────

def _score_temp(live: dict) -> float:
    """Higher score for cooler device. >85°C → 0, <40°C → 1."""
    temps = live.get("sample_temps_c", [])
    if not temps:
        temp = live.get("temp_c", live.get("temperature_c"))
        if temp is None:
            return 0.8   # no data → assume ok
        temps = [temp]
    max_temp = max(float(t) for t in temps if t is not None)
    if max_temp >= 85:
        return 0.0
    if max_temp <= 40:
        return 1.0
    return 1.0 - (max_temp - 40) / 45.0


def _score_load(live: dict) -> float:
    """Score based on CPU load + memory usage. Lower = better."""
    load = live.get("load1", live.get("cpu_load", live.get("load_percent")))
    mem = live.get("mem_used_percent", live.get("memory_used_percent"))
    scores = []
    if load is not None:
        # load1 as ratio of cpu count, or direct percent
        cpu_count = live.get("cpu_count", 1)
        norm = float(load) / float(cpu_count) if float(load) > 2 else float(load)
        scores.append(max(0.0, 1.0 - min(norm, 1.0)))
    if mem is not None:
        scores.append(max(0.0, 1.0 - float(mem) / 100.0))
    return sum(scores) / len(scores) if scores else 0.7


def _score_battery(live: dict) -> float:
    """Prefer devices with battery present and level ok."""
    level = live.get("level", live.get("battery_level"))
    if level is None:
        return 0.7   # no battery info → neutral
    return max(0.0, min(1.0, float(level) / 100.0))


def _score_rate(contract, role_spec: dict) -> float:
    """If role has a required rate and producer has a rate, score adequacy."""
    required = None
    spec_contract = role_spec.get("contract")
    if spec_contract and hasattr(spec_contract, "rate"):
        required = spec_contract.rate
    if required is None or contract is None:
        return 0.8   # no rate requirement → neutral
    if not hasattr(contract, "rate") or contract.rate is None:
        return 0.5
    ratio = float(contract.rate) / float(required)
    return min(1.0, ratio)
