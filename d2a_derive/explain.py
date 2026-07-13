"""
d2a_derive/explain.py — WHY the planner would pick the recipe it picks (Phase 6).

    python -m d2a_derive.explain <capability>

The observed-cost ranking (planner.py `_pick`) re-orders the recipes that provide a
capability by their measured history on THIS machine. That ranking is only as
trustworthy as it is legible, so this is the other half of the feature: a
deterministic, no-ML explanation the planner can print of its own decision.

For a capability it:

  1. shows whether a REAL provider would win outright (the strict invariant — real
     beats every derivation, and no measured reliability can lift a derived recipe
     over it); then
  2. lists every recipe that provides it, each with its lifetime metrics summary and
     its exact ranking key `(observed_score, cost_rank_hint, num_inputs)` — the SAME
     key `planner.ranking_key` sorts by, imported here so the explanation can never
     drift from the decision; then
  3. names the single DECIDING FACTOR between the top pick and the runner-up (which
     component of the key broke the tie), and lists any recipe excluded because it is
     quarantined.

HONEST SCOPE: this ranks the recipes for a name and explains the tie-break. It does
NOT re-run input-satisfiability discovery — a printed pick is "the recipe the planner
would prefer among those providing this name," and the caveat says so. The metrics it
ranks by measure this machine's history (its providers, its network, its load), not
the recipe's quality in the abstract: they inform the order, they do not certify it.
"""

import argparse
from pathlib import Path

from d2a import crypto
from d2a_derive.metrics import MetricsStore
from d2a_derive.planner import ranking_key
from d2a_derive.registry import Registry
from d2a_derive.trust import TrustStore


def _observed_factor(a: tuple, b: tuple) -> str | None:
    """Which component of the observed_score 3-tuple (failure_rate, heal_rate,
    mean_staleness) first differs between two recipes, named. None if identical."""
    labels = ("observed failure rate", "observed heal rate", "observed mean staleness")
    for av, bv, label in zip(a, b, labels):
        if av != bv:
            return label
    return None


def deciding_factor(metrics: MetricsStore, top, second) -> str:
    """Name the single reason `top` ranks ahead of `second` — the first position of
    the ranking key at which they differ, walked in the planner's own order."""
    kt, ks = ranking_key(metrics, top), ranking_key(metrics, second)
    obs = _observed_factor(kt[0], ks[0])
    if obs is not None:
        return f"lower {obs}"
    if kt[1] != ks[1]:
        return "lower author cost_rank_hint (no observed history separates them)"
    if kt[2] != ks[2]:
        return "fewer inputs (all else equal)"
    return "tie — kept in registry load order (nothing separates them)"


def explain(capability: str, *, recipes_dir=None, trust: TrustStore | None = None,
            metrics: MetricsStore | None = None,
            discover=None, include_quarantined: bool = False) -> dict:
    """
    Return a structured explanation of how the planner would rank the recipes that
    provide `capability`. Pure read: loads the registry + metrics store, never binds
    hardware. `discover` (optional) is only used to report whether a real provider
    would pre-empt derivation entirely; when omitted, no direct provider is assumed.

    Shape: {capability, direct_provider(bool), recipes[...ranked...], excluded[...],
    pick, runner_up, deciding_factor, cold_start(bool)}.
    """
    recipes_dir = Path(recipes_dir) if recipes_dir is not None \
        else (crypto.d2a_home() / "recipes")
    trust = trust if trust is not None else TrustStore()
    metrics = metrics if metrics is not None else MetricsStore()

    reg = Registry(recipes_dir=recipes_dir, trust=trust)
    all_for = reg.recipes_for(capability)

    # would a real provider win outright? (the strict, non-overridable invariant)
    direct = False
    if discover is not None:
        direct = any(isinstance(p, dict) and isinstance(p.get("manifest"), dict)
                     for p in (discover(capability) or []))

    excluded, candidates = [], []
    for lr in all_for:
        q = metrics.is_quarantined(lr.recipe_name)
        if q and not include_quarantined:
            excluded.append(lr)
        else:
            candidates.append(lr)
    candidates.sort(key=lambda lr: ranking_key(metrics, lr))

    def row(lr):
        m = metrics.get(lr.recipe_name)
        return {
            "recipe": lr.recipe_name,
            "provides": lr.provided_name,
            "cost_rank_hint": lr.cost_rank_hint,
            "num_inputs": len(lr.requires),
            "quarantined": metrics.is_quarantined(lr.recipe_name),
            "key": {"observed_score": list(m.observed_score()),
                    "cost_rank_hint": lr.cost_rank_hint,
                    "num_inputs": len(lr.requires)},
            "metrics": m.summary(),
        }

    ranked = [row(lr) for lr in candidates]
    cold = all(metrics.get(lr.recipe_name).runs == 0 for lr in candidates)
    factor = ""
    if len(candidates) >= 2:
        factor = deciding_factor(metrics, candidates[0], candidates[1])
    elif len(candidates) == 1:
        factor = "only one candidate — nothing to rank against"

    return {
        "capability": capability,
        "direct_provider": direct,
        "recipes": ranked,
        "excluded_quarantined": [lr.recipe_name for lr in excluded],
        "pick": candidates[0].recipe_name if candidates else None,
        "runner_up": candidates[1].recipe_name if len(candidates) >= 2 else None,
        "deciding_factor": factor,
        "cold_start": cold,
    }


def format_explanation(exp: dict) -> str:
    """Human-readable rendering of an explain() result."""
    cap = exp["capability"]
    out = [f"planner explanation for capability '{cap}'", "═" * 60]

    if exp["direct_provider"]:
        out.append("A REAL provider of this capability is discoverable → the planner")
        out.append("would use it DIRECTLY. No derivation is considered: a real provider")
        out.append("beats every recipe, and no measured reliability can override that.")
        out.append("")

    if not exp["recipes"] and not exp["excluded_quarantined"]:
        out.append("No recipe provides this capability (and none was excluded).")
        return "\n".join(out)

    if not exp["recipes"] and exp["excluded_quarantined"]:
        out.append("Every recipe providing this capability is QUARANTINED:")
        for n in exp["excluded_quarantined"]:
            out.append(f"  · {n}")
        out.append("The planner would refuse without include_quarantined "
                   "(re-run conformance to clear).")
        return "\n".join(out)

    out.append(f"{'':2}{'RANK':<5}{'RECIPE':<30}{'FAIL':>6}{'HEAL':>6}"
               f"{'STALE':>7}{'HINT':>6}{'IN':>4}{'RUNS':>6}")
    for i, r in enumerate(exp["recipes"], start=1):
        m = r["metrics"]
        mark = "▶" if i == 1 else " "
        out.append(f"{mark} {i:<5}{r['recipe']:<30}"
                   f"{m['failure_rate']:>6.2f}{m['heal_rate']:>6.2f}"
                   f"{m['mean_staleness_s']:>7.2f}{r['cost_rank_hint']:>6}"
                   f"{r['num_inputs']:>4}{m['runs']:>6}")

    out.append("")
    if exp["cold_start"]:
        out.append("COLD START: no run history for any candidate — ranked by the "
                   "author's cost_rank_hint alone (no data beats no data).")
    out.append(f"PICK       : {exp['pick']}")
    if exp["runner_up"]:
        out.append(f"runner-up  : {exp['runner_up']}")
    out.append(f"decided by : {exp['deciding_factor']}")
    if exp["excluded_quarantined"]:
        out.append(f"quarantined (excluded): {', '.join(exp['excluded_quarantined'])}")
    out.append("")
    out.append("Note: this ranks the recipes that PROVIDE this name and explains the")
    out.append("tie-break. Metrics measure THIS machine's history — they inform the")
    out.append("order, they do not certify a recipe. The structural preference (real >")
    out.append("single-hop > two-hop) and consent rule are never overridden by them.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m d2a_derive.explain",
        description="Explain WHY the planner would pick the recipe it picks for a "
                    "capability (observed-cost ranking, deterministic, no ML).")
    ap.add_argument("capability", help="the capability name to explain")
    ap.add_argument("--recipes-dir", default=None,
                    help="registry dir (default <d2a_home>/recipes)")
    ap.add_argument("--include-quarantined", action="store_true",
                    help="include quarantined recipes in the ranking (default: excluded)")
    args = ap.parse_args(argv)

    exp = explain(args.capability, recipes_dir=args.recipes_dir,
                  include_quarantined=args.include_quarantined)
    print(format_explanation(exp))
    return 0 if exp["pick"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
