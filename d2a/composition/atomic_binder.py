from __future__ import annotations
from typing import Callable, Tuple, List

from d2a.composition.cost_evaluator import Blueprint


class AtomicBinder:
    """
    Stage 8 — Atomic Binder.
    Binds every hop in a blueprint all-or-nothing.
    On any failure: rolls back every already-acquired binding in reverse order.
    NO half-pipelines ever leave the system.
    """

    def __init__(self, bind_fn: Callable, release_fn: Callable):
        # bind_fn(node_id, capability_name, priority) -> dict with "status" field
        # release_fn(binding_dict) -> None
        self._bind_fn = bind_fn
        self._release_fn = release_fn

    def bind_blueprint(
        self,
        blueprint: Blueprint,
        priority: int = 5,
    ) -> Tuple[bool, object]:
        """
        Attempt to bind every hop in the blueprint.
        Returns (True, list_of_bindings) on full success.
        Returns (False, reason_str) on any failure after rolling back all acquired bindings.
        Adapters are pure transforms — they require no binding.
        """
        acquired: List[dict] = []

        for hop in blueprint.hops:
            result = self._bind_fn(hop.node_id, hop.capability_name, priority)
            status = result.get("status", "error")

            if status not in ("granted", "granted_by_preemption"):
                # ── ROLLBACK: release everything acquired so far, reversed ──
                for binding in reversed(acquired):
                    try:
                        self._release_fn(binding)
                    except Exception:
                        pass  # idempotent: never throw during rollback
                reason = result.get("message", f"status={status}")
                return False, (
                    f"bind failed for {hop.node_id}/{hop.capability_name}: {reason}"
                )

            # Normalize binding for downstream release
            result["provider_node_id"] = hop.node_id
            result["capability_name"]  = hop.capability_name
            acquired.append(result)

        return True, acquired
