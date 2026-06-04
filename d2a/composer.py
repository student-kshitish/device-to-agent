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
from d2a.composition.atomic_binder import AtomicBinder
from d2a.composition.runtime_monitor import RuntimeMonitor
from d2a.composition.release_manager import ReleaseManager
from d2a.contracts import contracts_compatible


# ── CompositionPlan (Plan phase output) ───────────────────────────────────────

@dataclass
class CompositionPlan:
    goal: str
    ok: bool
    primary_blueprint: Optional[Blueprint] = None
    fallback_blueprints: List[Blueprint] = field(default_factory=list)
    stages_log: List[str] = field(default_factory=list)
    reason: str = ""

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


# ── Composition (Commit phase output; live handle) ────────────────────────────

class Composition:
    """
    Live handle returned by Composer.bind().
    Holds the bound blueprint, active bindings, runtime monitor, and release manager.
    Context-manager support: __exit__ calls release() automatically.

    with composer.bind(plan) as comp:
        comp.run()
    # all bindings freed here
    """

    def __init__(
        self,
        plan: CompositionPlan,
        bound_blueprint: Blueprint,
        bindings: List[dict],
        monitor: RuntimeMonitor,
        release_manager: ReleaseManager,
        binder: AtomicBinder,
        remaining_fallbacks: List[Blueprint],
        priority: int = 5,
    ):
        self.plan                  = plan
        self.bound_blueprint       = bound_blueprint
        self.bindings              = bindings          # parallel to bound_blueprint.hops
        self.monitor               = monitor
        self.release_manager       = release_manager
        self._binder               = binder
        self._remaining_fallbacks  = remaining_fallbacks
        self._priority             = priority
        self._released             = False

    # ── inspection ────────────────────────────────────────────────────────────

    def stages(self) -> list:
        """Return [(hop, binding), ...] for each stage in the bound blueprint."""
        return list(zip(self.bound_blueprint.hops, self.bindings))

    def check_health(self) -> dict:
        """On-demand health poll of all bound stages."""
        return self.monitor.check(self)

    def run(self, input_request=None) -> dict:
        """Execute the pipeline. Requires _composer to be set (done by Agent.achieve)."""
        if hasattr(self, "_composer"):
            return self._composer.run(self, input_request)
        return {"ok": False, "reason": "no composer attached — call Composer.run(composition)"}

    # ── release ───────────────────────────────────────────────────────────────

    def release(self) -> dict:
        if not self._released:
            self._released = True
            return self.release_manager.release_all(self)
        return {"released": [], "errors": [], "ok": True}

    # ── context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "Composition":
        return self

    def __exit__(self, *_) -> None:
        self.release()

    def __repr__(self) -> str:
        nodes = [h.node_id[:12] for h in self.bound_blueprint.hops]
        return (
            f"Composition(goal={self.plan.goal}, "
            f"nodes={nodes}, released={self._released})"
        )


# ── Composer ──────────────────────────────────────────────────────────────────

class Composer:
    """
    Orchestrates all 10 stages of Capability Composition.

    Stages 1-7  (Plan):   goal → contract-verified ranked blueprint + fallbacks.
    Stage  8    (Commit): atomic bind of all blueprint hops or none.
    Stage  9    (Operate): runtime health monitor, fallback re-bind.
    Stage  10   (Release): atomic release of all bindings.

    bind_fn(node_id, cap_name, priority) -> bind_result dict
    release_fn(binding_dict)             -> None
    health_fn(binding_dict)              -> {"verdict": str, "healthy": bool}
    data_fn(binding_dict)                -> frame dict  (producer data pull)
    """

    def __init__(
        self,
        capability_pool_provider: Callable[[], list],
        bind_fn:    Optional[Callable] = None,
        release_fn: Optional[Callable] = None,
        health_fn:  Optional[Callable] = None,
        data_fn:    Optional[Callable] = None,
    ):
        self._pool_provider    = capability_pool_provider
        self._bind_fn          = bind_fn
        self._release_fn       = release_fn
        self._health_fn        = health_fn
        self._data_fn          = data_fn

        # Stage pipeline instances
        self._goal_planner     = GoalPlanner()
        self._discovery        = Discovery()
        self._scorer           = CapabilityScorer()
        self._checker          = ContractChecker()
        self._adapter_gen      = AdapterGenerator()
        self._cost_evaluator   = CostEvaluator()
        self._fallback_planner = FallbackPlanner()

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 1-7: PLAN
    # ─────────────────────────────────────────────────────────────────────────

    def plan(self, goal: str) -> CompositionPlan:
        log: list[str] = []

        # Stage 1 — goal → role-specs
        try:
            role_specs = self._goal_planner.plan_requirements(goal)
        except ValueError as e:
            return CompositionPlan(goal=goal, ok=False, reason=str(e), stages_log=log)
        log.append(f"Stage 1 GoalPlanner: {len(role_specs)} role-specs for '{goal}'")

        # Stage 2 — discovery
        pool = self._pool_provider()
        candidates_by_role = self._discovery.find_providers(role_specs, pool)
        for i, specs in candidates_by_role.items():
            log.append(
                f"Stage 2 Discovery: role[{i}] "
                f"({role_specs[i]['role']}/{role_specs[i].get('media','any')}) "
                f"→ {len(specs)} candidates"
            )
        for i, cands in candidates_by_role.items():
            if not cands:
                spec = role_specs[i]
                return CompositionPlan(
                    goal=goal, ok=False,
                    reason=(
                        f"no provider found for role[{i}] "
                        f"role={spec['role']} media={spec.get('media','any')} "
                        f"label={spec.get('label','?')}"
                    ),
                    stages_log=log,
                )

        # Stage 3 — scoring + ranking
        ranked = {}
        for i, cands in candidates_by_role.items():
            ranked[i] = self._scorer.rank(cands, role_specs[i])
            top = ranked[i][0]
            log.append(
                f"Stage 3 Scorer: role[{i}] best={top.get('node_id','?')[:12]} "
                f"score={top.get('_score',0):.4f}"
            )

        # Stages 4-6 — contract check + adapter gen + cost evaluation
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
            reasons = list({b.reject_reason for b in blueprints if not b.valid})
            return CompositionPlan(
                goal=goal, ok=False,
                reason="no valid blueprint: " + "; ".join(reasons[:3]),
                stages_log=log,
            )

        # Stage 7 — fallback planning
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

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 8: BIND (atomic, with automatic fallback)
    # ─────────────────────────────────────────────────────────────────────────

    def bind(self, plan: CompositionPlan, priority: int = 5):
        """
        Stage 8 — Atomic bind.
        Tries primary blueprint first; on failure automatically tries each
        fallback blueprint in order. If all fail, returns (False, reason)
        having bound nothing. On success returns a Composition.
        """
        if not plan.ok:
            return False, f"cannot bind a failed plan: {plan.reason}"
        if self._bind_fn is None or self._release_fn is None:
            return False, "Composer has no bind_fn/release_fn — cannot commit"

        binder = AtomicBinder(self._bind_fn, self._release_fn)
        monitor = RuntimeMonitor(self._health_fn or _default_health_fn)
        release_manager = ReleaseManager(self._release_fn)

        # Ordered list to try: primary first, then fallbacks
        candidates = [plan.primary_blueprint] + list(plan.fallback_blueprints)
        last_reason = "no blueprints"

        for idx, blueprint in enumerate(candidates):
            label = "primary" if idx == 0 else f"fallback-{idx}"
            ok, result = binder.bind_blueprint(blueprint, priority)
            if ok:
                bindings = result
                remaining = candidates[idx + 1:]  # fallbacks not yet tried
                comp = Composition(
                    plan=plan,
                    bound_blueprint=blueprint,
                    bindings=bindings,
                    monitor=monitor,
                    release_manager=release_manager,
                    binder=binder,
                    remaining_fallbacks=remaining,
                    priority=priority,
                )
                return comp
            last_reason = f"{label}: {result}"

        return False, f"all blueprints failed to bind. Last: {last_reason}"

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 9: OPERATE (run pipeline + monitor + fallback)
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, composition: Composition, input_request=None) -> dict:
        """
        Stage 9 — Execute the bound pipeline end-to-end.
        1. Check health; if unhealthy attempt fallback re-bind (atomic).
        2. Pull data from producer via data_fn.
        3. Simulate adapter chain (contract tracking).
        4. Consumer CONFIRMS exact contract match — proving end-to-end guarantee.
        Returns {"ok", "stages_executed", "final_output_contract", "consumer_confirmed"}.
        """
        # ── pre-run health check ──────────────────────────────────────────────
        health = composition.monitor.check(composition)
        if not health.get("overall_healthy", True):
            action = composition.monitor.on_unhealthy(composition)
            if action == "fallback":
                ok = self._rebind_fallback(composition)
                if not ok:
                    return {"ok": False,
                            "reason": "composition unhealthy and no fallback available"}
            else:
                return {"ok": False, "reason": "composition unhealthy, no fallback available"}

        # ── pipeline execution ────────────────────────────────────────────────
        stages_executed: list = []
        current_contract = None
        frame = None

        for hop, binding in zip(composition.bound_blueprint.hops, composition.bindings):
            if hop.role == "producer":
                # Pull data from producer device
                if self._data_fn:
                    try:
                        frame = self._data_fn(binding)
                    except Exception as e:
                        frame = {"error": str(e)}

                # Simulate adapter chain: re-derive contract step by step
                current_contract = hop.contract_in
                for adapter in hop.adapter_chain:
                    current_contract = adapter.produces_for(current_contract)

                stages_executed.append({
                    "role":             "producer",
                    "node_id":          hop.node_id,
                    "capability":       hop.capability_name,
                    "contract_in":      hop.contract_in,
                    "adapters_applied": [a.describe() for a in hop.adapter_chain],
                    "contract_out":     current_contract,
                    "frame_pulled":     frame is not None and "error" not in (frame or {}),
                })

            elif hop.role == "consumer":
                # Consumer confirms exact contract match at runtime
                consumer_contract = hop.contract_in
                ok_compat, compat_reason = contracts_compatible(
                    current_contract, consumer_contract
                )
                if not ok_compat:
                    return {
                        "ok":               False,
                        "reason":           f"RUNTIME CONTRACT MISMATCH: {compat_reason}",
                        "consumer_confirmed": False,
                    }
                stages_executed.append({
                    "role":               "consumer",
                    "node_id":            hop.node_id,
                    "capability":         hop.capability_name,
                    "contract_expected":  consumer_contract,
                    "contract_received":  current_contract,
                    "consumer_confirmed": True,
                })

        return {
            "ok":                    True,
            "stages_executed":       stages_executed,
            "final_output_contract": current_contract,
            "consumer_confirmed":    True,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 10: RELEASE
    # ─────────────────────────────────────────────────────────────────────────

    def release(self, composition: Composition) -> dict:
        """Stage 10 — Delegate to ReleaseManager to free all bindings."""
        return composition.release()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _rebind_fallback(self, composition: Composition) -> bool:
        """
        Release current bindings and attempt to bind each remaining fallback blueprint.
        Updates composition in place on success.
        """
        # Release current bindings (without stopping monitor — we're about to rebind)
        try:
            composition.release_manager._release_fn  # ensure it exists
            for binding in list(composition.bindings):
                try:
                    composition._binder._release_fn(binding)
                except Exception:
                    pass
            composition.bindings.clear()
        except Exception:
            pass

        while composition._remaining_fallbacks:
            blueprint = composition._remaining_fallbacks.pop(0)
            ok, result = composition._binder.bind_blueprint(
                blueprint, composition._priority
            )
            if ok:
                composition.bound_blueprint = blueprint
                composition.bindings = result
                return True
        return False


# ── fallback health fn when none is provided ─────────────────────────────────

def _default_health_fn(binding: dict) -> dict:
    return {"verdict": "unknown", "healthy": True}
