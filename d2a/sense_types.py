"""
d2a/sense_types.py — D2A Sense Layer contracts.

CONCEPT: Bare hardware produces raw device-specific signals; agents want clean
intent-level data. The Sense Layer translates raw → the shape the agent asked
for, and translates agent intent → the right reads.

This module defines the shared types that flow through the entire pipeline so
every stage agrees on what is being passed.
"""

from dataclasses import dataclass
from typing import Any

# ── Shape constants ────────────────────────────────────────────────────────────
# Each shape is a strictly different view of the SAME physical read.
# "raw"        → dict of per-source dicts exactly as the kernel returned them.
# "normalized" → same structure, all numerics scaled to [0, 1].
# "features"   → flat vector + aligned names list; ready for ML inference.
# "verdict"    → {"verdict": str, "advice": str} — for dumb agents that need zero ML.
VALID_SHAPES = {"raw", "normalized", "features", "verdict"}

# ── Mode constants ─────────────────────────────────────────────────────────────
# "on_demand" → single read now; no background work.
# "urgent"    → skip optional pipeline stages; fastest possible path (Part 2).
# "monitor"   → repeated reads; signals the pipeline to keep buffers warm.
VALID_MODES = {"on_demand", "urgent", "monitor"}

# ── Verdict levels — ordered BEST to WORST health ─────────────────────────────
# Index position is significant: higher index = worse condition.
# comfort  → everything nominal; agent may proceed at full load.
# caution  → approaching limits; agent should throttle.
# strain   → heavy load; agent should reduce active work.
# distress → critical; agent MUST release this resource now.
# fatigue  → battery critically low; agent should prefer a plugged device.
VERDICT_LEVELS = ["comfort", "caution", "strain", "distress", "fatigue"]

# ── Advice — 1-to-1 with VERDICT_LEVELS ───────────────────────────────────────
# Each advice entry is the action an agent should take for the same-index verdict.
ADVICE = ["proceed", "throttle", "reduce_load", "release_now", "prefer_plugged_device"]


@dataclass
class SenseRequest:
    """What an agent asks the sense layer to produce for one resource read."""
    resource: str                      # capability name, e.g. "compute", "battery_aware"
    shape: str         = "normalized"  # one of VALID_SHAPES
    mode: str          = "on_demand"   # one of VALID_MODES
    model_hint: str | None = None      # optional tag for downstream ML routing / logging


@dataclass
class SenseFrame:
    """
    Complete output of one sense pipeline run.

    Always includes verdict + advice + confidence regardless of requested shape,
    so a caller requesting "raw" still gets the health judgment for free.

    vetoed / veto_reason are Part 2 fields (Safety Filter). They stay at their
    defaults (False / None) in all Part 1 frames.
    """
    resource: str
    shape: str
    data: Any           # shape-specific payload; None when resource is not offered
    verdict: str | None                 # one of VERDICT_LEVELS, or None if unavailable
    advice: str | None                  # one of ADVICE, aligned with verdict
    confidence: float                   # [0, 1] — sensory confidence across all sources
    device_class: str
    ts: float
    seq: int                            # monotonically increasing per resource
    # Part 2 Safety Filter populates these:
    vetoed: bool = False
    veto_reason: str | None = None
