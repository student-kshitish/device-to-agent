"""
d2a_derive/planner.py — the need() pipeline (Phase 1: plan only, no hardware).

need(name) walks the ten-component pipeline through the point of binding:

    1. DIRECT DISCOVERY FIRST — a real provider of `name` ALWAYS beats a derived
       substitute. If one exists, return outcome="direct" and stop.
    2. RECIPE MATCH — recipes whose provides.name == `name` (registry.recipes_for).
       None -> refused(NO_RECIPE).
    3. CONTRACT VALIDATION — for each matching recipe, every `requires` input must
       be satisfiable by a discovered provider (fields, types, units incl. declared
       adaptations, min_hz). No recipe satisfiable -> refused(CONTRACT_UNSATISFIED).
    4. COST RANKING — among satisfiable recipes pick the RICHEST: lowest
       cost_rank_hint, tie-broken by FEWEST inputs.
    5. DRY-RUN GATE — re-affirm the chosen recipe passed its own frames (it did, at
       registry admission; a defensive re-check keeps the gate in the pipeline).
    6. PLAN — compute the effective consent tier (structural max), build provenance,
       and return a DerivationPlan.

Phase 1 STOPS at the plan. Binding the inputs under leases, feeding the transform
via on_event, and exposing a live DerivedCapability is Phase 2. The plan is the
seam between the two.

DISCOVERY SEAM: `discover(capability_name) -> list[ProviderInfo]`, where each
ProviderInfo is a dict {node_id, name, manifest}. In Phase 2 this is backed by
RemoteAgent.find_capability + describe; in Phase 1 tests inject a fake so the
planner is exercised without standing up devices.
"""

from dataclasses import dataclass, field

from d2a_derive import errors
from d2a_derive.registry import Registry, LoadedRecipe
from d2a_derive.validator import check_input_against_provider

# open < sensitive. max() over this order is the structural consent rule.
_TIER_ORDER = {"open": 0, "sensitive": 1}
_ORDER_TIER = {v: k for k, v in _TIER_ORDER.items()}


def effective_tier(tiers) -> str:
    """The structural, non-overridable consent rule: the MAX tier over the inputs
    and the recipe's declared output tier. Unknown tiers are treated as the most
    sensitive (fail-safe)."""
    worst = 0
    for t in tiers:
        worst = max(worst, _TIER_ORDER.get(t, max(_TIER_ORDER.values())))
    return _ORDER_TIER[worst]


@dataclass
class Provenance:
    """Where a derived capability came from — attached to every plan so the
    lineage (which recipe, which author, which inputs, resulting tier) is never
    lost."""
    recipe: str
    version: str
    author_pubkey: str
    inputs: list          # [{"capability": hint, "node_id": ..., "provider_name": ...}]
    effective_tier: str


@dataclass
class DerivationPlan:
    """A ready-to-bind plan. Phase 2's executor turns this into a live
    DerivedCapability; Phase 1 hands it back from need() for inspection/testing."""
    provided_name: str
    recipe: LoadedRecipe
    inputs: list                          # [{"hint", "field_names", "provider"}]
    effective_tier: str
    manifest: dict                        # provides manifest with consent_tier = effective_tier
    provenance: Provenance
    fidelity: str
    cannot_detect: list
    cost_rank_hint: int
    num_inputs: int


@dataclass
class NeedResult:
    """Outcome of need(): a real provider (direct), a derivation plan (derived), or
    a coded refusal."""
    outcome: str                          # "direct" | "derived" | "refused"
    direct_providers: list = field(default_factory=list)
    plan: DerivationPlan | None = None
    code: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("direct", "derived")


def _provider_ok(p: dict) -> bool:
    """A discovered provider is usable only if it carries a manifest (derivation is
    manifest-gated — an un-manifested capability cannot be contract-checked; see
    README protocol-gap note)."""
    return isinstance(p, dict) and isinstance(p.get("manifest"), dict)


class Planner:
    def __init__(self, registry: Registry, discover):
        """discover(capability_name) -> list[ProviderInfo{node_id, name, manifest}]."""
        self.registry = registry
        self.discover = discover

    def need(self, name: str, constraints: dict | None = None) -> NeedResult:
        # 1. DIRECT DISCOVERY FIRST — real beats derived.
        direct = [p for p in (self.discover(name) or []) if _provider_ok(p)]
        if direct:
            return NeedResult(outcome="direct", direct_providers=direct)

        # 2. RECIPE MATCH on provides.name.
        recipes = self.registry.recipes_for(name)
        if not recipes:
            return NeedResult(outcome="refused", code=errors.NO_RECIPE,
                              detail=f"no recipe provides '{name}' and no direct provider exists")

        # 3. CONTRACT VALIDATION — collect satisfiable recipes with an input mapping.
        candidates: list[tuple[LoadedRecipe, list]] = []
        reasons: list[str] = []
        for lr in recipes:
            ok, mapping, reason = self._match_inputs(lr)
            if ok:
                candidates.append((lr, mapping))
            else:
                reasons.append(f"{lr.recipe_name}: {reason}")
        if not candidates:
            return NeedResult(outcome="refused", code=errors.CONTRACT_UNSATISFIED,
                              detail="; ".join(reasons))

        # 4. COST RANKING — richest first: lowest cost_rank_hint, then fewest inputs.
        candidates.sort(key=lambda c: (c[0].cost_rank_hint, len(c[0].requires)))
        lr, mapping = candidates[0]

        # 5. DRY-RUN GATE — re-affirm (admission already enforced it).
        if not lr.dry_run.ok:
            return NeedResult(outcome="refused", code=errors.DRYRUN_FAILED,
                              detail=lr.dry_run.reason)

        # 6. PLAN.
        return NeedResult(outcome="derived", plan=self._build_plan(lr, mapping))

    # ── internals ──────────────────────────────────────────────────────────────

    def _match_inputs(self, lr: LoadedRecipe) -> tuple[bool, list, str]:
        """For each `requires` input, discover a satisfying provider. Returns
        (ok, mapping, reason). mapping = [{"hint","field_names","provider"}]."""
        mapping = []
        for req in lr.requires:
            hint = req.get("capability_hint")
            candidates = [p for p in (self.discover(hint) or []) if _provider_ok(p)]
            if not candidates:
                return False, [], f"no provider found for input '{hint}'"
            chosen = None
            last_reason = "no candidate satisfied the contract"
            for p in candidates:
                ok, reason = check_input_against_provider(req, p["manifest"], lr.unit_adaptations)
                if ok:
                    chosen = p
                    break
                last_reason = f"input '{hint}': {reason}"
            if chosen is None:
                return False, [], last_reason
            mapping.append({
                "hint": hint,
                "field_names": sorted(req.get("fields", {})),
                "provider": chosen,
            })
        return True, mapping, "ok"

    def _build_plan(self, lr: LoadedRecipe, mapping: list) -> DerivationPlan:
        input_tiers = [m["provider"]["manifest"].get("consent_tier", "sensitive")
                       for m in mapping]
        declared = lr.manifest.get("consent_tier", "sensitive")
        eff = effective_tier(input_tiers + [declared])

        # The derived capability is registered locally at the EFFECTIVE tier, which
        # may be strictly higher than the recipe's declared output tier (escalation).
        manifest = {**lr.manifest, "consent_tier": eff}

        provenance = Provenance(
            recipe=lr.recipe_name,
            version=lr.version,
            author_pubkey=lr.author_pubkey,
            inputs=[{"capability": m["hint"],
                     "node_id": m["provider"].get("node_id"),
                     "provider_name": m["provider"].get("name")} for m in mapping],
            effective_tier=eff,
        )
        return DerivationPlan(
            provided_name=lr.provided_name,
            recipe=lr,
            inputs=mapping,
            effective_tier=eff,
            manifest=manifest,
            provenance=provenance,
            fidelity=lr.fidelity,
            cannot_detect=lr.cannot_detect,
            cost_rank_hint=lr.cost_rank_hint,
            num_inputs=len(mapping),
        )
