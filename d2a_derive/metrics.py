"""
d2a_derive/metrics.py — the per-recipe runtime metrics store (Phase 6).

The ten-component table (README) always listed a **cost optimizer** as deferred:
"cost_rank_hint is an author's guess; a real optimizer learns from runtime." Phase 6
makes it real using the data the system now *generates on its own* — the health
snapshots, heal counts, staleness, and conformance reports Phases 2–5 already produce.
This module is the memory that turns that stream into a small, honest, per-recipe
record the planner can rank by.

WHAT IS STORED (persisted at <d2a_home>/derive_metrics.json, one entry per recipe):

    runs             number of completed live runs (a DerivedCapability start→close)
    total_uptime     summed wall-clock seconds those runs stayed up
    heal_count       summed successful input rebinds across those runs (lease losses
                     the self-healer recovered from — a proxy for input flakiness)
    failed_count     runs that ended in the FAILED state (a required input died)
    mean_staleness   running mean of each run's mean input staleness (seconds)
    last_conformance {"passed": bool, "ts": float} — the most recent conformance verdict
    quarantined      a flag (see below); the planner refuses to plan a quarantined
                     recipe without an explicit opt-in

HONEST SCOPE (say it plainly, mirrored in the README): these metrics measure **THIS
machine's history with a recipe** — its providers, its network, its load — NOT the
recipe's quality in the abstract. A recipe that heals constantly here may be flawless
on a stabler LAN. They INFORM the planner's tie-breaking; they do NOT certify a recipe
and NEVER override the structural preference tiers (real > single-hop > two-hop) or the
consent rule. Fidelity honesty outranks measured reliability, always.

WRITE DISCIPLINE (load-bearing): the executor/healer/monitor update *in-memory*
accumulators on the live DerivedCapability at state transitions; the store is written
to DISK only once per run (at the FAILED transition or at close(), whichever comes
first) and once per conformance result. There are NO per-frame writes — a derivation
emitting hundreds of frames writes its metrics exactly once. Tests assert this bound.

DETERMINISTIC, NO ML: the observed score (below) is a plain lexicographic tuple over
the stored rates. It is fully explainable — see d2a_derive/explain.py — which is the
point: the planner must be able to say WHY it picked what it picked.
"""

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from d2a import crypto

METRICS_FILENAME = "derive_metrics.json"

# QUARANTINE THRESHOLD (documented, deterministic). A recipe is auto-quarantined when
# it has failed MORE THAN HALF of a MEANINGFUL number of runs on this machine — a
# single unlucky failure never quarantines (that would be trigger-happy and would
# punish cold starts). A conformance FAILURE quarantines regardless of run count
# (it is a direct, deliberate verdict); a conformance PASS is the only thing that
# clears the flag (re-verify to reinstate — see record_conformance).
QUARANTINE_FAILURE_RATE = 0.5
QUARANTINE_MIN_RUNS = 3


@dataclass
class RecipeMetrics:
    """One recipe's rolling record. Rates are derived, never stored, so they can
    never drift out of sync with the counts."""
    runs: int = 0
    total_uptime: float = 0.0
    heal_count: int = 0
    failed_count: int = 0
    mean_staleness: float = 0.0
    last_conformance: dict | None = None
    quarantined: bool = False

    @property
    def failure_rate(self) -> float:
        return (self.failed_count / self.runs) if self.runs else 0.0

    @property
    def heal_rate(self) -> float:
        return (self.heal_count / self.runs) if self.runs else 0.0

    def observed_score(self) -> tuple:
        """The planner's observed-cost key: LOWER is better, compared
        lexicographically — **failure rate, then heal rate, then mean staleness.**
        A recipe with NO history scores (0.0, 0.0, 0.0) and thus ties every other
        no-history recipe, falling through to the author's cost_rank_hint (cold start
        honest: no data beats no data). We only ever PENALISE observed badness — a
        flawless record scores the same (0,0,0) as no record, so measured history can
        demote a demonstrably-flaky recipe but never *promote* one above an untested
        peer on optimism alone."""
        return (self.failure_rate, self.heal_rate, self.mean_staleness)

    def to_dict(self) -> dict:
        return {
            "runs": self.runs,
            "total_uptime": round(self.total_uptime, 3),
            "heal_count": self.heal_count,
            "failed_count": self.failed_count,
            "mean_staleness": round(self.mean_staleness, 4),
            "last_conformance": self.last_conformance,
            "quarantined": self.quarantined,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecipeMetrics":
        if not isinstance(d, dict):
            return cls()
        lc = d.get("last_conformance")
        return cls(
            runs=int(d.get("runs", 0)),
            total_uptime=float(d.get("total_uptime", 0.0)),
            heal_count=int(d.get("heal_count", 0)),
            failed_count=int(d.get("failed_count", 0)),
            mean_staleness=float(d.get("mean_staleness", 0.0)),
            last_conformance=lc if isinstance(lc, dict) else None,
            quarantined=bool(d.get("quarantined", False)),
        )

    def summary(self) -> dict:
        """A flat, human/JSON-friendly view for health() and explain()."""
        return {
            "runs": self.runs,
            "total_uptime_s": round(self.total_uptime, 2),
            "heal_count": self.heal_count,
            "failed_count": self.failed_count,
            "failure_rate": round(self.failure_rate, 3),
            "heal_rate": round(self.heal_rate, 3),
            "mean_staleness_s": round(self.mean_staleness, 3),
            "quarantined": self.quarantined,
            "last_conformance": self.last_conformance,
        }


class MetricsStore:
    """The persisted map recipe_name -> RecipeMetrics. Load-modify-save on the whole
    (small, one-line-per-recipe) file, mirroring TrustStore. Never raises on a write
    failure — metrics are advisory; losing a run's record must not break a derivation.
    `writes` counts disk saves so tests can assert the no-per-frame-write bound."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None \
            else (crypto.d2a_home() / METRICS_FILENAME)
        self._lock = threading.RLock()
        self._recipes: dict[str, RecipeMetrics] = {}
        self.writes = 0
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (ValueError, OSError):
            return
        recs = data.get("recipes") if isinstance(data, dict) else None
        if isinstance(recs, dict):
            self._recipes = {str(k): RecipeMetrics.from_dict(v) for k, v in recs.items()}

    def _save(self) -> None:
        payload = {"recipes": {k: v.to_dict() for k, v in sorted(self._recipes.items())}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))
            self.writes += 1
        except OSError:
            pass                      # advisory store — never break a run on a write fault

    # ── reads ────────────────────────────────────────────────────────────────────

    def get(self, recipe: str) -> RecipeMetrics:
        """The record for `recipe` (a fresh empty one if unseen — NOT persisted until
        a record_* call writes it, so a cold recipe leaves no file footprint)."""
        with self._lock:
            return self._recipes.get(recipe) or RecipeMetrics()

    def is_quarantined(self, recipe: str) -> bool:
        with self._lock:
            rec = self._recipes.get(recipe)
            return bool(rec and rec.quarantined)

    def observed_score(self, recipe: str) -> tuple:
        return self.get(recipe).observed_score()

    def all_names(self) -> list[str]:
        with self._lock:
            return sorted(self._recipes)

    # ── writes (bounded: one save per call, no per-frame path) ────────────────────

    def record_run(self, recipe: str, *, uptime: float, heal_count: int,
                   failed: bool, staleness: float) -> RecipeMetrics:
        """Fold one completed live run into the recipe's rolling record and persist
        (one disk write). Called ONCE per DerivedCapability lifetime — at the FAILED
        transition or at close() — never per frame."""
        with self._lock:
            rec = self._recipes.setdefault(recipe, RecipeMetrics())
            rec.runs += 1
            rec.total_uptime += max(0.0, float(uptime))
            rec.heal_count += max(0, int(heal_count))
            if failed:
                rec.failed_count += 1
            # running mean of per-run mean staleness, denominator = runs.
            sample = max(0.0, float(staleness))
            rec.mean_staleness += (sample - rec.mean_staleness) / rec.runs
            self._maybe_quarantine(rec)
            self._save()
            return rec

    def record_conformance(self, recipe: str, passed: bool, ts: float | None = None) -> RecipeMetrics:
        """Record a conformance verdict. A PASS is the authoritative all-clear — it
        clears any quarantine (the documented way to reinstate a recipe). A FAIL
        quarantines directly, regardless of run history."""
        import time as _t
        with self._lock:
            rec = self._recipes.setdefault(recipe, RecipeMetrics())
            rec.last_conformance = {"passed": bool(passed),
                                    "ts": float(ts if ts is not None else _t.time())}
            rec.quarantined = not passed
            self._save()
            return rec

    def clear(self, recipe: str) -> None:
        """Drop a recipe's record entirely (e.g. after uninstall). Persists."""
        with self._lock:
            if recipe in self._recipes:
                del self._recipes[recipe]
                self._save()

    # ── quarantine policy ─────────────────────────────────────────────────────────

    @staticmethod
    def _maybe_quarantine(rec: RecipeMetrics) -> None:
        """Engage the auto-quarantine when the failure rate crosses the documented
        threshold over a meaningful number of runs. Never auto-CLEARS here — only a
        passing conformance run clears the flag (record_conformance)."""
        if rec.runs >= QUARANTINE_MIN_RUNS and rec.failure_rate > QUARANTINE_FAILURE_RATE:
            rec.quarantined = True
