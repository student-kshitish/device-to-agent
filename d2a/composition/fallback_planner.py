from __future__ import annotations
from d2a.composition.cost_evaluator import Blueprint


class FallbackPlanner:
    def plan(
        self,
        blueprints: list[Blueprint],
        max_fallbacks: int = 3,
    ) -> tuple[Blueprint | None, list[Blueprint]]:
        """
        Return (primary, fallbacks) from sorted valid blueprints.
        Fallbacks prefer different providers than primary to survive device failures.
        """
        valid = sorted(
            [b for b in blueprints if b.valid],
            key=lambda b: b.total_cost,
        )
        if not valid:
            return None, []

        primary = valid[0]
        primary_ids = set(primary.provider_ids())

        # Prefer fallbacks that don't share any provider with primary.
        # If not enough, accept partial overlap.
        disjoint = [
            b for b in valid[1:]
            if not set(b.provider_ids()) & primary_ids
        ]
        overlapping = [
            b for b in valid[1:]
            if set(b.provider_ids()) & primary_ids
        ]

        fallbacks = (disjoint + overlapping)[:max_fallbacks]
        return primary, fallbacks
