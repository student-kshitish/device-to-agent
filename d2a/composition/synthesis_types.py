"""
d2a/composition/synthesis_types.py — data types for CASE 3: Synthesis Layer.

CORE PRINCIPLE: the parts become NEURONS of one body; the BRAIN is always
the agent.  Synthesis does NOT create intelligence — it fuses passive parts
into one addressable virtual resource.  The agent remains the single mind.
"""

from __future__ import annotations
from dataclasses import dataclass, field

# ── named policy constants (no magic numbers) ─────────────────────────────────
POOLED_FILL_STRATEGY = "fill_sequential"  # fill member 0 first, then member 1, …
TIERED_HOT_POLICY    = "lru"              # eviction policy for fast tier
TIERED_FAST_MAX      = 8                  # max entries in fast tier before LRU eviction


@dataclass
class SynthesisSpec:
    """
    Describes HOW to fuse a set of members into one emergent device.
    Data-driven: adding a new synthesis type = adding one entry to SYNTHESIS_REGISTRY.
    """
    kind:         str        # e.g. "pooled_storage", "tiered_memory"
    member_roles: list[str]  # role labels members must carry (order matters for tiered)
    policy:       dict       # combination rules — fully named, fully inspectable
    min_members:  int = 1    # minimum valid member count; below this → refuse


@dataclass
class EmergentDevice:
    """
    Blueprint of a fused virtual device — the output of synthesis planning.
    NOT yet bound; describes what will exist once all members are bound.
    The device itself has NO logic: all routing lives in EmergentDeviceHandle.
    """
    name:             str
    kind:             str
    members:          list[dict]   # pool candidate dicts for each selected member
    combined_contract: dict        # e.g. {"total_bytes": N, "members": M}
    placement_map:    dict         # virtual → physical mapping (member indices / tiers)
    live_state:       dict


@dataclass
class SynthesisPlan:
    """
    Intermediate result of Synthesizer.plan_synthesis().
    Carries the EmergentDevice blueprint; converted to Blueprint objects
    for the standard FallbackPlanner before returning a CompositionPlan.
    """
    spec:            SynthesisSpec
    emergent_device: EmergentDevice
    selected:        list[dict]   # the actual pool entries chosen
    ok:              bool
    reason:          str = ""


# ── registry ──────────────────────────────────────────────────────────────────
# Extend by adding entries here.  The Synthesizer reads this at plan time.

SYNTHESIS_REGISTRY: dict[str, SynthesisSpec] = {
    "pooled_storage": SynthesisSpec(
        kind="pooled_storage",
        member_roles=["storage_member"],
        policy={
            "combine":   "concat_capacity",
            "placement": POOLED_FILL_STRATEGY,
        },
        min_members=1,
    ),
    "tiered_memory": SynthesisSpec(
        kind="tiered_memory",
        member_roles=["fast_tier", "slow_tier"],
        policy={
            "hot_policy":       TIERED_HOT_POLICY,
            "fast_max_entries": TIERED_FAST_MAX,
        },
        min_members=2,  # needs exactly one fast + one slow
    ),
}
