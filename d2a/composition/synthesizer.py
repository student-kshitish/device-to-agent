"""
d2a/composition/synthesizer.py — plan-phase sub-stage for synthesis goals.

CORE PRINCIPLE: the parts become NEURONS of one body; the BRAIN is always
the agent.  Synthesis does NOT create intelligence — it fuses passive parts
into one addressable virtual resource.  The agent remains the single mind.

Synthesis is a PLAN-PHASE sub-stage: it runs INSIDE Composer.plan() and
produces Blueprint objects that flow into the normal FallbackPlanner.
NOTHING is bound until the full plan passes verification.
"""

from __future__ import annotations
from d2a.contracts import IOContract
from d2a.composition.cost_evaluator import Blueprint, HopRecord
from d2a.composition.synthesis_types import (
    SYNTHESIS_REGISTRY,
    SynthesisSpec,
    EmergentDevice,
    SynthesisPlan,
)

# Contract used for all storage members — a new media type for block/fs storage.
_STORAGE_CONTRACT = IOContract(media="storage", format="raw_block")

# ── scoring weights for storage candidates ────────────────────────────────────
_W_FREE_BYTES  = 0.6   # more free space = better
_W_RELIABILITY = 0.4   # writable + available = more reliable


class Synthesizer:
    """
    Plan-phase sub-stage: maps a synthesis goal to a set of Blueprint objects
    (each representing a valid member configuration) for the FallbackPlanner.

    All rules are explicit and inspectable — no hidden heuristics.
    """

    def can_synthesize(self, goal: str) -> bool:
        return goal in SYNTHESIS_REGISTRY

    def enumerate_synthesis_plans(
        self, goal: str, candidate_pool: list[dict]
    ) -> list[Blueprint]:
        """
        For a synthesis goal, return a list of Blueprint objects representing
        different valid member configurations.  The FallbackPlanner then picks
        primary + fallbacks from this list.

        For pooled_storage: primary = all N members; fallbacks = subsets of N-1, N-2 …
        For tiered_memory:  exactly one blueprint (one fast + one slow; no fallback).
        Returns [] if no valid configuration exists.
        """
        spec = SYNTHESIS_REGISTRY[goal]

        if spec.kind == "pooled_storage":
            return self._enumerate_pooled(spec, goal, candidate_pool)
        if spec.kind == "tiered_memory":
            return self._enumerate_tiered(spec, goal, candidate_pool)

        return []  # unknown kind — fail cleanly

    # ── pooled_storage ────────────────────────────────────────────────────────

    def _enumerate_pooled(
        self, spec: SynthesisSpec, goal: str, pool: list[dict]
    ) -> list[Blueprint]:
        members = [c for c in pool if c.get("role") == "storage_member"]
        if not members:
            return [Blueprint(
                goal=goal, hops=[], total_cost=1e9, valid=False,
                reject_reason=f"pooled_storage needs ≥{spec.min_members} storage_member, found 0",
            )]

        # Score members by free bytes + writability
        scored = [dict(m, _score=self._score_storage(m)) for m in members]
        scored.sort(key=lambda m: m["_score"], reverse=True)

        blueprints: list[Blueprint] = []
        # Primary: all members; fallbacks: drop lowest-scored one at a time
        for n in range(len(scored), spec.min_members - 1, -1):
            subset = scored[:n]
            bp = self._build_pooled_blueprint(spec, goal, subset)
            blueprints.append(bp)

        if not blueprints:
            return [Blueprint(
                goal=goal, hops=[], total_cost=1e9, valid=False,
                reject_reason=f"pooled_storage: insufficient valid members (min={spec.min_members})",
            )]
        return blueprints

    def _build_pooled_blueprint(
        self, spec: SynthesisSpec, goal: str, members: list[dict]
    ) -> Blueprint:
        hops: list[HopRecord] = []
        placement_map: dict = {}
        offset = 0

        for i, m in enumerate(members):
            free_bytes = m.get("live_state", {}).get("free_bytes", 0)
            hops.append(HopRecord(
                role_index=i,
                node_id=m["node_id"],
                capability_name=m.get("capability", "raw_storage"),
                role="storage_member",
                contract_in=_STORAGE_CONTRACT,
                adapter_chain=[],
                contract_out=_STORAGE_CONTRACT,
                score=m.get("_score", 0.5),
                adapter_cost=0.0,
            ))
            placement_map[i] = {
                "member_index": i,
                "node_id":      m["node_id"],
                "byte_range":   (offset, offset + free_bytes),
                "relay_ref":    m.get("relay_ref"),
            }
            offset += free_bytes

        emergent = EmergentDevice(
            name=f"pooled_storage_{len(members)}x",
            kind="pooled_storage",
            members=members,
            combined_contract={
                "media":       "storage",
                "total_bytes": offset,
                "members":     len(members),
            },
            placement_map=placement_map,
            live_state={
                "total_bytes":  offset,
                "member_count": len(members),
            },
        )

        avg_score  = sum(m.get("_score", 0.5) for m in members) / len(members)
        total_cost = round(1.0 - avg_score, 4)

        return Blueprint(
            goal=goal,
            hops=hops,
            total_cost=total_cost,
            valid=True,
            synthesis_metadata={"emergent_device": emergent, "kind": "pooled_storage"},
        )

    # ── tiered_memory ─────────────────────────────────────────────────────────

    def _enumerate_tiered(
        self, spec: SynthesisSpec, goal: str, pool: list[dict]
    ) -> list[Blueprint]:
        fast_members = [c for c in pool if c.get("role") == "fast_tier"]
        slow_members = [c for c in pool if c.get("role") == "slow_tier"]

        if not fast_members:
            return [Blueprint(
                goal=goal, hops=[], total_cost=1e9, valid=False,
                reject_reason="tiered_memory needs ≥1 fast_tier member, found 0",
            )]
        if not slow_members:
            return [Blueprint(
                goal=goal, hops=[], total_cost=1e9, valid=False,
                reject_reason="tiered_memory needs ≥1 slow_tier member, found 0",
            )]

        # Score and pick best fast + best slow
        best_fast = max(fast_members, key=self._score_storage)
        best_slow = max(slow_members, key=self._score_storage)

        fast_scored = dict(best_fast, _score=self._score_storage(best_fast))
        slow_scored = dict(best_slow, _score=self._score_storage(best_slow))

        bp = self._build_tiered_blueprint(spec, goal, fast_scored, slow_scored)
        return [bp]

    def _build_tiered_blueprint(
        self, spec: SynthesisSpec, goal: str,
        fast: dict, slow: dict,
    ) -> Blueprint:
        fast_bytes = fast.get("live_state", {}).get("free_bytes", 0)
        slow_bytes = slow.get("live_state", {}).get("free_bytes", 0)

        hops = [
            HopRecord(
                role_index=0,
                node_id=fast["node_id"],
                capability_name=fast.get("capability", "raw_storage"),
                role="fast_tier",
                contract_in=_STORAGE_CONTRACT,
                adapter_chain=[],
                contract_out=_STORAGE_CONTRACT,
                score=fast.get("_score", 0.5),
                adapter_cost=0.0,
            ),
            HopRecord(
                role_index=1,
                node_id=slow["node_id"],
                capability_name=slow.get("capability", "raw_storage"),
                role="slow_tier",
                contract_in=_STORAGE_CONTRACT,
                adapter_chain=[],
                contract_out=_STORAGE_CONTRACT,
                score=slow.get("_score", 0.5),
                adapter_cost=0.0,
            ),
        ]

        emergent = EmergentDevice(
            name="tiered_memory",
            kind="tiered_memory",
            members=[fast, slow],
            combined_contract={
                "media":      "storage",
                "fast_bytes": fast_bytes,
                "slow_bytes": slow_bytes,
            },
            placement_map={
                "fast": {
                    "node_id":  fast["node_id"],
                    "relay_ref": fast.get("relay_ref"),
                    "max_entries": spec.policy.get("fast_max_entries", 8),
                },
                "slow": {
                    "node_id":  slow["node_id"],
                    "relay_ref": slow.get("relay_ref"),
                },
            },
            live_state={
                "fast_bytes":   fast_bytes,
                "slow_bytes":   slow_bytes,
                "hot_policy":   spec.policy.get("hot_policy", "lru"),
                "fast_max":     spec.policy.get("fast_max_entries", 8),
            },
        )

        total_cost = round(
            (1.0 - fast.get("_score", 0.5)) + (1.0 - slow.get("_score", 0.5)), 4
        )

        return Blueprint(
            goal=goal,
            hops=hops,
            total_cost=total_cost,
            valid=True,
            synthesis_metadata={"emergent_device": emergent, "kind": "tiered_memory"},
        )

    # ── scoring ───────────────────────────────────────────────────────────────

    def _score_storage(self, candidate: dict) -> float:
        """Score a storage candidate 0..1. More free bytes + writable = better."""
        live      = candidate.get("live_state", {})
        free      = float(live.get("free_bytes", 0))
        size      = float(live.get("size_bytes", max(free, 1)))
        free_frac = free / size if size > 0 else 0.5
        writable  = 1.0 if live.get("writable", True) else 0.0
        return round(_W_FREE_BYTES * free_frac + _W_RELIABILITY * writable, 4)

    # ── verification helper ───────────────────────────────────────────────────

    def verify_fusion(self, members: list[dict], spec: SynthesisSpec) -> tuple[bool, str]:
        """
        Verify that the selected members can be fused per the spec.
        Returns (ok, reason).  Fails explicitly — never silently degrades.
        """
        if len(members) < spec.min_members:
            return False, (
                f"{spec.kind} needs ≥{spec.min_members} members, got {len(members)}"
            )
        # All members must have valid live_state (at least some free space)
        for m in members:
            if m.get("live_state", {}).get("free_bytes", -1) < 0:
                return False, (
                    f"member {m.get('node_id','?')} has invalid free_bytes"
                )
        return True, "ok"
