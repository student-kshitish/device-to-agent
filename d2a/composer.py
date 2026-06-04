from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Any

from d2a.composition.goal_planner import GoalPlanner
from d2a.composition.discovery import Discovery
from d2a.composition.scorer import CapabilityScorer
from d2a.composition.contract_checker import ContractChecker
from d2a.composition.adapter_generator import AdapterGenerator
from d2a.composition.cost_evaluator import CostEvaluator, Blueprint
from d2a.composition.fallback_planner import FallbackPlanner


@dataclass
class CompositionPlan:
    goal: str
    ok: bool
    primary_blueprint: Optional[Blueprint] = None
    fallback_blueprints: List[Blueprint] = field(default_factory=list)
    stages_log: List[str] = field(default_factory=list)
    reason: str = ""           # set when ok=False

    def describe(self) -> str:
        lines = [f"=== CompositionPlan goal={self.goal} ok={self.ok} ==="]
        if not self.ok:
            lines.append(f"REJECTED: {self.reason}")
            return "\n".join(lines)
        lines.append("\n--- PRIMARY BLUEPRINT ---")
        lines.append(self.primary_blueprint.describe())
        for i, fb in enumerate(self.fallback_blueprints):
            lines.append(f"\n--- FALLBACK {i+1} ---")
            lines.append(fb.describe())
        lines.append("\n--- STAGES LOG ---")
        lines.extend(f"  {s}" for s in self.stages_log)
        return "\n".join(lines)


class Composer:
    """
    Orchestrates the 7-stage PLAN phase (stages 1–7).
    Accepts a callable that returns the current capability pool.

    # TODO Part 2: attach bind(plan) / run(plan) / release(plan) here.
    # bind()    → negotiate with each node, reserve resources, issue tokens
    # run()     → wire data channels according to blueprint hop order
    # release() → unbind all hops in reverse order, free resources
    """

    def __init__(self, capability_pool_provider: Callable[[], list[dict]]):
        self._pool_provider = capability_pool_provider
        self._goal_planner    = GoalPlanner()
        self._discovery       = Discovery()
        self._scorer          = CapabilityScorer()
        self._checker         = ContractChecker()
        self._adapter_gen     = AdapterGenerator()
        self._cost_evaluator  = CostEvaluator()
        self._fallback_planner = FallbackPlanner()

    def plan(self, goal: str) -> CompositionPlan:
        log: list[str] = []

        # ── Stage 1: goal → role-specs ────────────────────────────────────────
        try:
            role_specs = self._goal_planner.plan_requirements(goal)
        except ValueError as e:
            return CompositionPlan(goal=goal, ok=False, reason=str(e), stages_log=log)
        log.append(f"Stage 1 GoalPlanner: {len(role_specs)} role-specs for '{goal}'")

        # ── Stage 2: discovery ────────────────────────────────────────────────
        pool = self._pool_provider()
        candidates_by_role = self._discovery.find_providers(role_specs, pool)
        for i, specs in candidates_by_role.items():
            log.append(
                f"Stage 2 Discovery: role[{i}] ({role_specs[i]['role']}/{role_specs[i].get('media','any')}) "
                f"→ {len(specs)} candidates"
            )
        # Fail fast: if any role has zero candidates
        for i, cands in candidates_by_role.items():
            if not cands:
                spec = role_specs[i]
                reason = (
                    f"no provider found for role[{i}] "
                    f"role={spec['role']} media={spec.get('media','any')} "
                    f"label={spec.get('label','?')}"
                )
                return CompositionPlan(goal=goal, ok=False, reason=reason, stages_log=log)

        # ── Stage 3: scoring + ranking ────────────────────────────────────────
        ranked = {}
        for i, cands in candidates_by_role.items():
            ranked[i] = self._scorer.rank(cands, role_specs[i])
            top = ranked[i][0]
            log.append(
                f"Stage 3 Scorer: role[{i}] best={top.get('node_id','?')[:12]} "
                f"score={top.get('_score',0):.4f}"
            )

        # ── Stages 4+5+6: enumerate blueprints (contract check + adapter gen + cost) ──
        blueprints = self._cost_evaluator.enumerate_blueprints(
            ranked, self._checker, self._adapter_gen, goal=goal
        )
        valid_count = sum(1 for b in blueprints if b.valid)
        log.append(
            f"Stage 4-6 ContractCheck+AdapterGen+CostEval: "
            f"{len(blueprints)} blueprints enumerated, {valid_count} valid"
        )

        best = self._cost_evaluator.best(blueprints)
        if best is None:
            # Collect reasons from invalid blueprints for diagnosis
            reasons = list({b.reject_reason for b in blueprints if not b.valid})
            return CompositionPlan(
                goal=goal,
                ok=False,
                reason="no valid blueprint: " + "; ".join(reasons[:3]),
                stages_log=log,
            )

        # ── Stage 7: fallback planning ────────────────────────────────────────
        primary, fallbacks = self._fallback_planner.plan(blueprints)
        log.append(
            f"Stage 7 FallbackPlanner: primary cost={primary.total_cost:.3f} "
            f"fallbacks={len(fallbacks)}"
        )

        return CompositionPlan(
            goal=goal,
            ok=True,
            primary_blueprint=primary,
            fallback_blueprints=fallbacks,
            stages_log=log,
        )
