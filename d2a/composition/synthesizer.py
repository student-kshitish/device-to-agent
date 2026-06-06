"""
d2a/composition/synthesizer.py — plan-phase sub-stage for synthesis goals.

CORE PRINCIPLE: the parts become NEURONS of one body; the BRAIN is always
the agent.  Synthesis does NOT create intelligence — it fuses passive parts
into one addressable virtual resource.  The agent remains the single mind.

Synthesis is a PLAN-PHASE sub-stage: it runs INSIDE Composer.plan() and
produces Blueprint objects that flow into the normal FallbackPlanner.
NOTHING is bound until the full plan passes verification.

SENSITIVE-kind rule: candidates whose kind is sensitive (from KIND_SENSITIVITY)
OR whose access == "consent_required" are EXCLUDED from any synthesis with a
clear reject reason.  No sensitive member is ever silently fused.
Pool entries without a 'kind' field are treated as open (backward-compatible
with existing storage pool entries that predate this field).
"""

from __future__ import annotations
from d2a.contracts import IOContract
from d2a.composition.cost_evaluator import Blueprint, HopRecord
from d2a.composition.synthesis_types import (
    SYNTHESIS_REGISTRY,
    SynthesisSpec,
    EmergentDevice,
    SynthesisPlan,
    MERGED_STREAM_POLICY,
    SENSOR_ARRAY_AGG,
)

# Contracts for member types
_STORAGE_CONTRACT = IOContract(media="storage", format="raw_block")
_STREAM_CONTRACT  = IOContract(media="stream",  format="raw_bytes")
_SENSOR_CONTRACT  = IOContract(media="scalar",  format="raw_text")

# Scoring weights for storage candidates
_W_FREE_BYTES  = 0.6
_W_RELIABILITY = 0.4


class Synthesizer:
    """
    Plan-phase sub-stage: maps a synthesis goal to Blueprint objects
    (each representing a valid member configuration) for the FallbackPlanner.

    All rules are explicit and inspectable — no hidden heuristics.
    Device-kind checking reuses device_kinds.py constants — no duplication.
    """

    def can_synthesize(self, goal: str) -> bool:
        return goal in SYNTHESIS_REGISTRY

    def enumerate_synthesis_plans(
        self, goal: str, candidate_pool: list[dict]
    ) -> list[Blueprint]:
        """
        Return Blueprint objects for all valid member configurations.
        Primary blueprint = most members; fallbacks = subsets (for pooled/merged/sensor).
        Returns [] → invalid blueprint with reason if no valid config exists.
        """
        spec = SYNTHESIS_REGISTRY[goal]

        if spec.kind == "pooled_storage":
            return self._enumerate_pooled(spec, goal, candidate_pool)
        if spec.kind == "tiered_memory":
            return self._enumerate_tiered(spec, goal, candidate_pool)
        if spec.kind == "merged_stream":
            return self._enumerate_merged_stream(spec, goal, candidate_pool)
        if spec.kind == "sensor_array":
            return self._enumerate_sensor_array(spec, goal, candidate_pool)

        return [Blueprint(
            goal=goal, hops=[], total_cost=1e9, valid=False,
            reject_reason=f"unknown synthesis kind: {spec.kind}",
        )]

    # ── sensitive-member filter ───────────────────────────────────────────────

    def _filter_sensitive(
        self, candidates: list[dict]
    ) -> tuple[list[dict], list[tuple[str, str]]]:
        """
        Separate candidates into (allowed, [(node_id, reason), ...]).

        Excluded when ANY of:
          - candidate.access == "consent_required"  (relay reported consent needed)
          - KIND_SENSITIVITY[candidate.kind] == "sensitive"  (kind is inherently sensitive)

        Pool entries without 'kind'/'access' fields default to open (backward compat).
        """
        from d2a.guardian.device_kinds import KIND_SENSITIVITY
        allowed:  list[dict]               = []
        excluded: list[tuple[str, str]]    = []
        for c in candidates:
            nid    = c.get("node_id", "?")
            access = c.get("access", "open")
            kind   = c.get("kind", "")
            if access == "consent_required":
                excluded.append((nid, "sensitive member needs consent (access=consent_required)"))
            elif KIND_SENSITIVITY.get(kind, "open") == "sensitive":
                excluded.append((nid, f"kind '{kind}' is sensitive — explicit consent required"))
            else:
                allowed.append(c)
        return allowed, excluded

    def _filter_by_kind(
        self,
        candidates: list[dict],
        required_kind: str | None,
    ) -> tuple[list[dict], list[tuple[str, str]]]:
        """
        Filter candidates by required member_kind.
        Candidates without a 'kind' field pass (backward compat).
        """
        if required_kind is None:
            return candidates, []
        allowed:  list[dict]            = []
        excluded: list[tuple[str, str]] = []
        for c in candidates:
            kind = c.get("kind")
            if kind is None or kind == required_kind:
                allowed.append(c)
            else:
                excluded.append((
                    c.get("node_id", "?"),
                    f"member kind '{kind}' is not '{required_kind}'"
                    + (f" ('{kind}' is a sensitive kind requiring consent)"
                       if kind == "input_event" else ""),
                ))
        return allowed, excluded

    # ── pooled_storage ────────────────────────────────────────────────────────

    def _enumerate_pooled(
        self, spec: SynthesisSpec, goal: str, pool: list[dict]
    ) -> list[Blueprint]:
        role_pool  = [c for c in pool if c.get("role") == "storage_member"]
        kind_ok, kind_excl = self._filter_by_kind(role_pool, spec.member_kind)
        sens_ok, sens_excl = self._filter_sensitive(kind_ok)
        all_excl   = kind_excl + sens_excl
        members    = sens_ok

        if not members:
            reason = (f"pooled_storage needs ≥{spec.min_members} storage_member"
                      f", found 0")
            if all_excl:
                reason += "; excluded: " + "; ".join(f"{n}: {r}" for n, r in all_excl[:3])
            return [Blueprint(goal=goal, hops=[], total_cost=1e9, valid=False,
                              reject_reason=reason)]

        scored = [dict(m, _score=self._score_storage(m)) for m in members]
        scored.sort(key=lambda m: m["_score"], reverse=True)

        blueprints: list[Blueprint] = []
        for n in range(len(scored), spec.min_members - 1, -1):
            subset = scored[:n]
            bp = self._build_pooled_blueprint(spec, goal, subset, all_excl)
            blueprints.append(bp)

        if not blueprints:
            return [Blueprint(goal=goal, hops=[], total_cost=1e9, valid=False,
                              reject_reason=f"pooled_storage: insufficient valid members (min={spec.min_members})")]
        return blueprints

    def _build_pooled_blueprint(
        self, spec: SynthesisSpec, goal: str,
        members: list[dict], excluded_members: list | None = None,
    ) -> Blueprint:
        hops: list[HopRecord] = []
        placement_map: dict   = {}
        offset = 0

        for i, m in enumerate(members):
            free_bytes = m.get("live_state", {}).get("free_bytes", 0)
            hops.append(HopRecord(
                role_index=i,
                node_id=m["node_id"],
                capability_name=m.get("capability", "raw_block_fs"),
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
            live_state={"total_bytes": offset, "member_count": len(members)},
        )

        avg_score  = sum(m.get("_score", 0.5) for m in members) / len(members)
        total_cost = round(1.0 - avg_score, 4)

        return Blueprint(
            goal=goal, hops=hops, total_cost=total_cost, valid=True,
            synthesis_metadata={
                "emergent_device":  emergent,
                "kind":             "pooled_storage",
                "excluded_members": excluded_members or [],
            },
        )

    # ── tiered_memory ─────────────────────────────────────────────────────────

    def _enumerate_tiered(
        self, spec: SynthesisSpec, goal: str, pool: list[dict]
    ) -> list[Blueprint]:
        fast_all = [c for c in pool if c.get("role") == "fast_tier"]
        slow_all = [c for c in pool if c.get("role") == "slow_tier"]

        fast_kind_ok, fast_excl = self._filter_by_kind(fast_all, spec.member_kind)
        slow_kind_ok, slow_excl = self._filter_by_kind(slow_all, spec.member_kind)
        fast_ok, fast_sens_excl = self._filter_sensitive(fast_kind_ok)
        slow_ok, slow_sens_excl = self._filter_sensitive(slow_kind_ok)
        all_excl = fast_excl + fast_sens_excl + slow_excl + slow_sens_excl

        if not fast_ok:
            return [Blueprint(goal=goal, hops=[], total_cost=1e9, valid=False,
                              reject_reason="tiered_memory needs ≥1 fast_tier member, found 0")]
        if not slow_ok:
            return [Blueprint(goal=goal, hops=[], total_cost=1e9, valid=False,
                              reject_reason="tiered_memory needs ≥1 slow_tier member, found 0")]

        best_fast = max(fast_ok, key=self._score_storage)
        best_slow = max(slow_ok, key=self._score_storage)

        fast_scored = dict(best_fast, _score=self._score_storage(best_fast))
        slow_scored = dict(best_slow, _score=self._score_storage(best_slow))

        bp = self._build_tiered_blueprint(spec, goal, fast_scored, slow_scored, all_excl)
        return [bp]

    def _build_tiered_blueprint(
        self, spec: SynthesisSpec, goal: str,
        fast: dict, slow: dict,
        excluded_members: list | None = None,
    ) -> Blueprint:
        fast_bytes = fast.get("live_state", {}).get("free_bytes", 0)
        slow_bytes = slow.get("live_state", {}).get("free_bytes", 0)

        hops = [
            HopRecord(
                role_index=0, node_id=fast["node_id"],
                capability_name=fast.get("capability", "raw_block_fs"),
                role="fast_tier",
                contract_in=_STORAGE_CONTRACT, adapter_chain=[],
                contract_out=_STORAGE_CONTRACT,
                score=fast.get("_score", 0.5), adapter_cost=0.0,
            ),
            HopRecord(
                role_index=1, node_id=slow["node_id"],
                capability_name=slow.get("capability", "raw_block_fs"),
                role="slow_tier",
                contract_in=_STORAGE_CONTRACT, adapter_chain=[],
                contract_out=_STORAGE_CONTRACT,
                score=slow.get("_score", 0.5), adapter_cost=0.0,
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
                    "node_id":    fast["node_id"],
                    "relay_ref":  fast.get("relay_ref"),
                    "max_entries": spec.policy.get("fast_max_entries", 8),
                },
                "slow": {
                    "node_id":   slow["node_id"],
                    "relay_ref": slow.get("relay_ref"),
                },
            },
            live_state={
                "fast_bytes": fast_bytes, "slow_bytes": slow_bytes,
                "hot_policy": spec.policy.get("hot_policy", "lru"),
                "fast_max":   spec.policy.get("fast_max_entries", 8),
            },
        )

        total_cost = round(
            (1.0 - fast.get("_score", 0.5)) + (1.0 - slow.get("_score", 0.5)), 4
        )
        return Blueprint(
            goal=goal, hops=hops, total_cost=total_cost, valid=True,
            synthesis_metadata={
                "emergent_device":  emergent,
                "kind":             "tiered_memory",
                "excluded_members": excluded_members or [],
            },
        )

    # ── merged_stream ─────────────────────────────────────────────────────────

    def _enumerate_merged_stream(
        self, spec: SynthesisSpec, goal: str, pool: list[dict]
    ) -> list[Blueprint]:
        role_pool  = [c for c in pool if c.get("role") == "stream_member"]
        kind_ok, kind_excl = self._filter_by_kind(role_pool, spec.member_kind)
        sens_ok, sens_excl = self._filter_sensitive(kind_ok)
        all_excl   = kind_excl + sens_excl
        members    = sens_ok

        if len(members) < spec.min_members:
            reason = (f"merged_stream needs ≥{spec.min_members} char_stream stream_member"
                      f", found {len(members)}")
            if all_excl:
                reason += "; excluded: " + "; ".join(f"{n}: {r}" for n, r in all_excl[:3])
            return [Blueprint(goal=goal, hops=[], total_cost=1e9, valid=False,
                              reject_reason=reason)]

        blueprints: list[Blueprint] = []
        for n in range(len(members), spec.min_members - 1, -1):
            subset = members[:n]
            bp = self._build_merged_blueprint(spec, goal, subset, all_excl)
            blueprints.append(bp)
        return blueprints

    def _build_merged_blueprint(
        self, spec: SynthesisSpec, goal: str,
        members: list[dict], excluded_members: list | None = None,
    ) -> Blueprint:
        hops = [
            HopRecord(
                role_index=i, node_id=m["node_id"],
                capability_name=m.get("capability", "raw_char_stream"),
                role="stream_member",
                contract_in=_STREAM_CONTRACT, adapter_chain=[],
                contract_out=_STREAM_CONTRACT,
                score=0.8, adapter_cost=0.0,
            )
            for i, m in enumerate(members)
        ]
        placement_map = {
            i: {"node_id": m["node_id"], "relay_ref": m.get("relay_ref")}
            for i, m in enumerate(members)
        }
        emergent = EmergentDevice(
            name=f"merged_stream_{len(members)}x",
            kind="merged_stream",
            members=members,
            combined_contract={
                "media":        "stream",
                "members":      len(members),
                "merge_policy": spec.policy.get("merge", MERGED_STREAM_POLICY),
            },
            placement_map=placement_map,
            live_state={"member_count": len(members)},
        )
        return Blueprint(
            goal=goal, hops=hops, total_cost=0.2, valid=True,
            synthesis_metadata={
                "emergent_device":  emergent,
                "kind":             "merged_stream",
                "excluded_members": excluded_members or [],
            },
        )

    # ── sensor_array ──────────────────────────────────────────────────────────

    def _enumerate_sensor_array(
        self, spec: SynthesisSpec, goal: str, pool: list[dict]
    ) -> list[Blueprint]:
        role_pool  = [c for c in pool if c.get("role") == "sensor_member"]
        kind_ok, kind_excl = self._filter_by_kind(role_pool, spec.member_kind)
        sens_ok, sens_excl = self._filter_sensitive(kind_ok)
        all_excl   = kind_excl + sens_excl
        members    = sens_ok

        if len(members) < spec.min_members:
            reason = (f"sensor_array needs ≥{spec.min_members} sensor_file sensor_member"
                      f", found {len(members)}")
            if all_excl:
                reason += "; excluded: " + "; ".join(f"{n}: {r}" for n, r in all_excl[:3])
            return [Blueprint(goal=goal, hops=[], total_cost=1e9, valid=False,
                              reject_reason=reason)]

        blueprints: list[Blueprint] = []
        for n in range(len(members), spec.min_members - 1, -1):
            subset = members[:n]
            bp = self._build_sensor_array_blueprint(spec, goal, subset, all_excl)
            blueprints.append(bp)
        return blueprints

    def _build_sensor_array_blueprint(
        self, spec: SynthesisSpec, goal: str,
        members: list[dict], excluded_members: list | None = None,
    ) -> Blueprint:
        hops = [
            HopRecord(
                role_index=i, node_id=m["node_id"],
                capability_name=m.get("capability", "raw_sensor_file"),
                role="sensor_member",
                contract_in=_SENSOR_CONTRACT, adapter_chain=[],
                contract_out=_SENSOR_CONTRACT,
                score=0.9, adapter_cost=0.0,
            )
            for i, m in enumerate(members)
        ]
        placement_map = {
            i: {
                "node_id":   m["node_id"],
                "member_id": m.get("node_id", f"member_{i}"),
                "relay_ref": m.get("relay_ref"),
            }
            for i, m in enumerate(members)
        }
        emergent = EmergentDevice(
            name=f"sensor_array_{len(members)}x",
            kind="sensor_array",
            members=members,
            combined_contract={
                "media":     "sensor_array",
                "members":   len(members),
                "aggregate": spec.policy.get("aggregate", SENSOR_ARRAY_AGG),
            },
            placement_map=placement_map,
            live_state={"member_count": len(members)},
        )
        return Blueprint(
            goal=goal, hops=hops, total_cost=0.1, valid=True,
            synthesis_metadata={
                "emergent_device":  emergent,
                "kind":             "sensor_array",
                "excluded_members": excluded_members or [],
            },
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
        if spec.kind in ("pooled_storage", "tiered_memory"):
            for m in members:
                if m.get("live_state", {}).get("free_bytes", -1) < 0:
                    return False, (
                        f"member {m.get('node_id','?')} has invalid free_bytes"
                    )
        return True, "ok"
