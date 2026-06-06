"""
d2a/composition/synthesis_types.py — data types for CASE 3: Synthesis Layer.

CORE PRINCIPLE: the parts become NEURONS of one body; the BRAIN is always
the agent.  Synthesis does NOT create intelligence — it fuses passive parts
into one addressable virtual resource.  The agent remains the single mind.

Device-kind consistency (same kind strings as device_kinds.py):
  pooled_storage → block_fs members   (concat capacity)
  tiered_memory  → block_fs members   (fast + slow tier)
  merged_stream  → char_stream members (interleaved stream)
  sensor_array   → sensor_file members (per-member + aggregate reads)

SENSITIVE-kind rule: input_event members are never silently fused into any
synthesis — they are excluded with a clear reason.  Reuses KIND_SENSITIVITY
from device_kinds.py; no separate gate is invented here.
"""

from __future__ import annotations
from dataclasses import dataclass, field

# ── named policy constants (no magic numbers) ─────────────────────────────────
POOLED_FILL_STRATEGY = "fill_sequential"   # fill member 0 first, then member 1, …
TIERED_HOT_POLICY    = "lru"               # eviction policy for fast tier
TIERED_FAST_MAX      = 8                   # max entries in fast tier before LRU eviction
MERGED_STREAM_POLICY = "interleave"        # merge policy: round-robin by member order
SENSOR_ARRAY_AGG     = "stats"             # aggregate: min / max / mean across members


@dataclass
class SynthesisSpec:
    """
    Describes HOW to fuse a set of members into one emergent device.
    Data-driven: adding a new synthesis type = adding one entry to SYNTHESIS_REGISTRY.

    member_kind: the device_kinds.py kind string that member relays must have.
                 None = no kind constraint (backward-compatible with existing specs
                 whose pool entries predate this field).
    """
    kind:         str
    member_roles: list[str]   # role labels members must carry (order matters for tiered)
    policy:       dict        # combination rules — fully named, fully inspectable
    min_members:  int = 1     # minimum valid member count; below this → refuse
    member_kind:  str | None = None   # device kind required (device_kinds constants)


@dataclass
class EmergentDevice:
    """
    Blueprint of a fused virtual device — the output of synthesis planning.
    NOT yet bound; describes what will exist once all members are bound.
    The device itself has NO logic: all routing lives in EmergentDeviceHandle.
    """
    name:             str
    kind:             str
    members:          list[dict]    # pool candidate dicts for each selected member
    combined_contract: dict         # e.g. {"total_bytes": N, "members": M}
    placement_map:    dict          # virtual → physical mapping (member indices / tiers)
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
    selected:        list[dict]    # the actual pool entries chosen
    ok:              bool
    reason:          str = ""


# ── registry ──────────────────────────────────────────────────────────────────
# Extend by adding entries here.  The Synthesizer reads this at plan time.

SYNTHESIS_REGISTRY: dict[str, SynthesisSpec] = {
    # ── storage fusions (block_fs members) ───────────────────────────────────
    "pooled_storage": SynthesisSpec(
        kind="pooled_storage",
        member_roles=["storage_member"],
        policy={
            "combine":   "concat_capacity",
            "placement": POOLED_FILL_STRATEGY,
        },
        min_members=1,
        member_kind="block_fs",
    ),
    "tiered_memory": SynthesisSpec(
        kind="tiered_memory",
        member_roles=["fast_tier", "slow_tier"],
        policy={
            "hot_policy":       TIERED_HOT_POLICY,
            "fast_max_entries": TIERED_FAST_MAX,
        },
        min_members=2,   # needs exactly one fast + one slow
        member_kind="block_fs",
    ),
    # ── stream fusion (char_stream members) ──────────────────────────────────
    "merged_stream": SynthesisSpec(
        kind="merged_stream",
        member_roles=["stream_member"],
        policy={
            "merge": MERGED_STREAM_POLICY,
        },
        min_members=1,
        member_kind="char_stream",
    ),
    # ── sensor array (sensor_file members) ───────────────────────────────────
    "sensor_array": SynthesisSpec(
        kind="sensor_array",
        member_roles=["sensor_member"],
        policy={
            "aggregate": SENSOR_ARRAY_AGG,
        },
        min_members=1,
        member_kind="sensor_file",
    ),
}
