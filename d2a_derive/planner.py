"""
d2a_derive/planner.py — the need() pipeline (with Phase-4 multi-hop chaining).

need(name) resolves a capability with a STRICT PREFERENCE ORDER:

    real provider  >  single-hop derived  >  two-hop chain

1. DIRECT DISCOVERY FIRST — a real provider of `name` always beats a derivation.
2. RECIPE MATCH — recipes whose provides.name == `name`. None → refused(NO_RECIPE).
3a. SINGLE-HOP PASS — a recipe whose EVERY input is satisfiable by a real discovered
    provider (fields, types, units incl. declared adaptations, min_hz vs declared hz).
3b. CHAINED PASS — only if single-hop found nothing AND a hop remains in the depth
    budget: an unmet input may itself be DERIVED (an inner plan), contract-checked at
    the seam exactly as if the inner's provides manifest were a discovered provider.
4. COST RANKING within the chosen tier — lowest cost_rank_hint, then fewest inputs.
5. DRY-RUN GATE — re-affirm the chosen recipe passed its own frames.
6. PLAN — effective tier = MAX across the chain; cannot_detect = UNION of all hops;
   fidelity concatenated; provenance NESTS the inner's (full lineage readable).

GUARDS (Phase 4): MAX_DERIVATION_DEPTH bounds the number of stacked derivations (a
safety rail — confidence compounds, trust surface widens). A recipe may not
transitively require its own provides (DERIVATION_CYCLE); a chain deeper than the
rail is DEPTH_EXCEEDED. On-wire chaining needs NO planner change — a PUBLISHED
derived capability is just an ordinary provider to discover().

The plan is the seam to the executor (Phase 2), which binds direct inputs under
leases and instantiates inner plans locally for chained ones.

DISCOVERY SEAM: `discover(capability_name) -> list[ProviderInfo]`, where each
ProviderInfo is a dict {node_id, name, manifest}. Backed by RemoteAgent.
find_capability + describe live; tests inject a fake to exercise the planner without
standing up devices.
"""

from dataclasses import dataclass, field

from d2a_derive import errors
from d2a_derive.metrics import MetricsStore
from d2a_derive.registry import Registry, LoadedRecipe
from d2a_derive.validator import check_input_against_provider

# open < sensitive. max() over this order is the structural consent rule.
_TIER_ORDER = {"open": 0, "sensitive": 1}
_ORDER_TIER = {v: k for k, v in _TIER_ORDER.items()}

# MAX_DERIVATION_DEPTH — the number of stacked DERIVATIONS (hops), not bindings, the
# planner will chain. 2 means "a recipe may be fed by ONE derived input, which is
# itself fed by real providers" (compute → presence → activity_summary) but no
# deeper. This is a deliberate SAFETY RAIL, not a technical limit: every hop
# compounds fidelity loss (each recipe is a coarse proxy of a coarse proxy), widens
# the trust surface (you now trust every author in the lineage), and multiplies the
# debugging cost when the top-level number looks wrong. Raise it only with eyes open.
MAX_DERIVATION_DEPTH = 2


def ranking_key(metrics: MetricsStore, lr: LoadedRecipe) -> tuple:
    """The planner's within-tier ordering key — lowest-first, lexicographic:

        (observed_score)  →  (cost_rank_hint)  →  (num_inputs)

    where observed_score = (failure_rate, heal_rate, mean_staleness) from the metrics
    store. Exposed as a MODULE FUNCTION so `_pick` (the decision) and explain.py (the
    explanation of the decision) rank by the EXACT same key — the explanation can
    never drift from what the planner actually does. A no-history recipe scores
    (0,0,0) and falls through to the author's cost_rank_hint (cold start honest)."""
    return (metrics.observed_score(lr.recipe_name), lr.cost_rank_hint, len(lr.requires))


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
    lost. For a CHAINED derivation, an input's dict carries `derived_from`: the
    nested Provenance of the inner derivation that produces it, so the FULL lineage
    (all hops, all authors) is readable from the top."""
    recipe: str
    version: str
    author_pubkey: str
    inputs: list          # [{"capability", "node_id", "provider_name", derived_from?}]
    effective_tier: str

    def lineage_lines(self, _indent: int = 0) -> list[str]:
        """Flatten the full chain into human-readable indented lines (top → leaves)."""
        pad = "  " * _indent
        out = [f"{pad}{self.recipe} (author {self.author_pubkey[:12]}…, tier {self.effective_tier})"]
        for inp in self.inputs:
            sub = inp.get("derived_from")
            if isinstance(sub, Provenance):
                out.append(f"{pad}  ← {inp['capability']} [DERIVED]:")
                out.extend(sub.lineage_lines(_indent + 2))
            else:
                node = (inp.get("node_id") or "?")[:8]
                out.append(f"{pad}  ← {inp['capability']} [real provider {node}]")
        return out


@dataclass
class DerivationPlan:
    """A ready-to-bind plan. Phase 2's executor turns this into a live
    DerivedCapability; Phase 1 hands it back from need() for inspection/testing.

    An input mapping entry is EITHER direct — carries `provider` (a discovered
    provider dict) — OR chained — carries `inner_plan` (a nested DerivationPlan the
    executor instantiates locally to produce that input). `depth` is the number of
    derivation hops in this subtree (1 = single-hop; 2 = one chained input)."""
    provided_name: str
    recipe: LoadedRecipe
    inputs: list                          # [{"hint","field_names", provider|inner_plan}]
    effective_tier: str
    manifest: dict                        # provides manifest w/ chain-max tier + unioned cannot_detect
    provenance: Provenance
    fidelity: str
    cannot_detect: list
    cost_rank_hint: int
    num_inputs: int
    depth: int = 1


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
    def __init__(self, registry: Registry, discover, *,
                 metrics: MetricsStore | None = None,
                 include_quarantined: bool = False):
        """discover(capability_name) -> list[ProviderInfo{node_id, name, manifest}].

        Phase 6: `metrics` is the observed-runtime store used to (a) rank recipes
        within a preference tier by observed cost and (b) EXCLUDE quarantined recipes
        from candidacy unless `include_quarantined` is set. An empty store (the cold
        default, and every fresh test home) leaves ranking identical to Phase 1 — a
        no-history recipe scores (0,0,0) and falls through to cost_rank_hint."""
        self.registry = registry
        self.discover = discover
        self.metrics = metrics if metrics is not None else MetricsStore()
        self.include_quarantined = include_quarantined

    def need(self, name: str, constraints: dict | None = None) -> NeedResult:
        """Public entry: the top of a chain is derivation level 1."""
        return self._need(name, _level=1, _chain=())

    def _need(self, name: str, _level: int, _chain: tuple) -> NeedResult:
        """
        Resolve `name` with STRICT PREFERENCE (tested per tier):
            real provider  >  single-hop derived  >  two-hop chain
        Never chains when a shorter path satisfies. `_level` is this derivation's
        hop number (1 = top); `_chain` is the tuple of capability names already
        being derived upstream (cycle guard).
        """
        # CYCLE GUARD — this capability is already being derived above us.
        if name in _chain:
            return NeedResult(outcome="refused", code=errors.DERIVATION_CYCLE,
                              detail="cycle: " + " → ".join(_chain + (name,)))

        # 1. DIRECT DISCOVERY FIRST — real beats every derivation.
        direct = [p for p in (self.discover(name) or []) if _provider_ok(p)]
        if direct:
            return NeedResult(outcome="direct", direct_providers=direct)

        # 2. RECIPE MATCH on provides.name.
        recipes = self.registry.recipes_for(name)
        if not recipes:
            return NeedResult(outcome="refused", code=errors.NO_RECIPE,
                              detail=f"no recipe provides '{name}' and no direct provider exists")

        # QUALITY GATE (Phase 6) — exclude QUARANTINED recipes from candidacy unless
        # the caller opted in. A recipe is quarantined by a failure rate over the
        # documented threshold or by a failed conformance run (metrics.py). This is
        # NEVER silent: excluded names are surfaced in the refusal reason, and a
        # recipe is never silently USED either (opt-in is explicit).
        quarantined_names: list[str] = []
        if not self.include_quarantined:
            kept = [lr for lr in recipes
                    if not self.metrics.is_quarantined(lr.recipe_name)]
            quarantined_names = [lr.recipe_name for lr in recipes
                                 if self.metrics.is_quarantined(lr.recipe_name)]
            recipes = kept
        if not recipes:
            return NeedResult(
                outcome="refused", code=errors.RECIPE_QUARANTINED,
                detail=f"all recipe(s) providing '{name}' are quarantined "
                       f"({', '.join(quarantined_names)}) — re-run conformance to clear, "
                       f"or plan with include_quarantined")

        # DEPTH RAIL — a real provider (handled above) is fine at any depth, but a
        # DERIVATION at this level must stay within the rail. This belts the PASS-2
        # gate below: even a level>MAX derivation fed entirely by real leaves is
        # refused, so the chain can never stack more than MAX_DERIVATION_DEPTH hops.
        if _level > MAX_DERIVATION_DEPTH:
            return NeedResult(outcome="refused", code=errors.DEPTH_EXCEEDED,
                              detail=f"deriving '{name}' would exceed max depth "
                                     f"{MAX_DERIVATION_DEPTH}")

        chain_here = _chain + (name,)
        # seed the reason trail with any quarantine exclusions so a later refusal
        # still names them (never silently skipped).
        reasons: list[str] = [f"{n}: quarantined (excluded)" for n in quarantined_names]

        # 3a. SINGLE-HOP PASS — every input satisfied by a REAL discovered provider.
        singles = []
        for lr in recipes:
            ok, mapping, reason, _ = self._match_inputs(lr, chain_here, _level, allow_chain=False)
            if ok:
                singles.append((lr, mapping))
            else:
                reasons.append(f"{lr.recipe_name}: {reason}")
        if singles:
            return self._pick(singles, _level)

        # 3b. CHAINED PASS — allow deriving an input, iff a hop remains in the budget.
        if _level < MAX_DERIVATION_DEPTH:
            chained = []
            inner_codes: set = set()
            for lr in recipes:
                ok, mapping, reason, codes = self._match_inputs(
                    lr, chain_here, _level, allow_chain=True)
                if ok:
                    chained.append((lr, mapping))
                else:
                    reasons.append(f"{lr.recipe_name}: {reason}")
                    inner_codes |= codes
            if chained:
                return self._pick(chained, _level)
            # nothing satisfiable even with chaining — surface the most specific code.
            if errors.DERIVATION_CYCLE in inner_codes:
                return NeedResult(outcome="refused", code=errors.DERIVATION_CYCLE,
                                  detail="; ".join(reasons))
            if errors.DEPTH_EXCEEDED in inner_codes:
                return NeedResult(outcome="refused", code=errors.DEPTH_EXCEEDED,
                                  detail="; ".join(reasons))
            return NeedResult(outcome="refused", code=errors.CONTRACT_UNSATISFIED,
                              detail="; ".join(reasons))

        # 3c. AT THE DEPTH CEILING — single-hop failed and we cannot chain deeper.
        # An unmet input that is ALREADY in the chain is a cycle (reported as such
        # even though the rail also blocks it); one that is merely derivable means we
        # stopped only because of the depth rail; otherwise it is a genuine dead end.
        code = self._ceiling_reason(recipes, chain_here)
        if code:
            return NeedResult(outcome="refused", code=code, detail="; ".join(reasons))
        return NeedResult(outcome="refused", code=errors.CONTRACT_UNSATISFIED,
                          detail="; ".join(reasons))

    def _pick(self, candidates: list, level: int) -> NeedResult:
        """Rank within this preference tier, dry-run gate, plan.

        RANKING KEY (Phase 6), lowest-first, lexicographic:
            (observed score)  →  (cost_rank_hint)  →  (fewest inputs)
        where observed score = (failure_rate, heal_rate, mean_staleness) from the
        metrics store. All candidates here are the SAME preference tier (this method
        is only reached inside one pass — single-hop OR chained), so observed cost
        only ever re-orders *within* a tier. The STRICT INVARIANT holds structurally:
        metrics can never lift a derived recipe over a real provider (chosen in step
        1) nor a two-hop chain over a single hop (separate passes) — fidelity honesty
        outranks measured reliability. A no-history recipe scores (0,0,0) and falls
        through to the author's cost_rank_hint (cold start honest)."""
        candidates.sort(key=lambda c: ranking_key(self.metrics, c[0]))
        lr, mapping = candidates[0]
        if not lr.dry_run.ok:
            return NeedResult(outcome="refused", code=errors.DRYRUN_FAILED,
                              detail=lr.dry_run.reason)
        return NeedResult(outcome="derived", plan=self._build_plan(lr, mapping, level))

    # ── internals ──────────────────────────────────────────────────────────────

    def _match_inputs(self, lr: LoadedRecipe, chain: tuple, level: int,
                      allow_chain: bool) -> tuple[bool, list, str, set]:
        """
        Satisfy every `requires` input. A real discovered provider is always tried
        first; only when none satisfies AND allow_chain do we DERIVE the input (an
        inner plan). Returns (ok, mapping, reason, inner_refusal_codes). A mapping
        entry carries `provider` (direct) or `inner_plan` (chained).
        """
        mapping = []
        inner_codes: set = set()
        for req in lr.requires:
            hint = req.get("capability_hint")
            fnames = sorted(req.get("fields", {}))

            # (a) real provider first — real beats derived, always.
            chosen = None
            last_reason = f"no provider found for input '{hint}'"
            for p in (p for p in (self.discover(hint) or []) if _provider_ok(p)):
                ok, reason = check_input_against_provider(req, p["manifest"], lr.unit_adaptations)
                if ok:
                    chosen = {"hint": hint, "field_names": fnames, "provider": p}
                    break
                last_reason = f"input '{hint}': {reason}"

            # (b) no real provider — DERIVE the input if allowed and a hop remains.
            if chosen is None and allow_chain:
                inner = self._need(hint, _level=level + 1, _chain=chain)
                if inner.outcome == "derived":
                    ok, reason = check_input_against_provider(
                        req, inner.plan.manifest, lr.unit_adaptations)
                    if ok:
                        chosen = {"hint": hint, "field_names": fnames,
                                  "inner_plan": inner.plan}
                    else:
                        last_reason = f"derived input '{hint}' seam mismatch: {reason}"
                elif inner.outcome == "refused":
                    inner_codes.add(inner.code)
                    last_reason = f"input '{hint}' underivable: {inner.code}"

            if chosen is None:
                return False, [], last_reason, inner_codes
            mapping.append(chosen)
        return True, mapping, "ok", inner_codes

    def _ceiling_reason(self, recipes: list, chain: tuple) -> str:
        """At the depth ceiling, classify WHY the single-hop pass failed:
        DERIVATION_CYCLE if an unmet input is already being derived upstream (would
        revisit it), else DEPTH_EXCEEDED if an unmet input is itself derivable (only
        the rail stopped us), else "" (a genuine dead end → contract-unsatisfied)."""
        depth_hit = False
        for lr in recipes:
            for req in lr.requires:
                hint = req.get("capability_hint")
                real = [p for p in (self.discover(hint) or []) if _provider_ok(p)]
                satisfied = any(
                    check_input_against_provider(req, p["manifest"], lr.unit_adaptations)[0]
                    for p in real)
                if satisfied:
                    continue
                if hint in chain:
                    return errors.DERIVATION_CYCLE
                if self.registry.recipes_for(hint):
                    depth_hit = True
        return errors.DEPTH_EXCEEDED if depth_hit else ""

    def _build_plan(self, lr: LoadedRecipe, mapping: list, level: int) -> DerivationPlan:
        # Split inputs into direct providers and chained inner plans.
        inners = [m["inner_plan"] for m in mapping if "inner_plan" in m]

        # effective tier = MAX ACROSS THE WHOLE CHAIN.
        input_tiers = [m["provider"]["manifest"].get("consent_tier", "sensitive")
                       for m in mapping if "provider" in m]
        input_tiers += [ip.effective_tier for ip in inners]
        declared = lr.manifest.get("consent_tier", "sensitive")
        eff = effective_tier(input_tiers + [declared])

        # cannot_detect = UNION of all hops (order-preserving dedupe).
        cannot = list(lr.cannot_detect)
        for ip in inners:
            for c in ip.cannot_detect:
                if c not in cannot:
                    cannot.append(c)

        # fidelity = concatenated hop-by-hop (top first, then each inner's).
        fidelity = lr.fidelity
        for ip in inners:
            fidelity = f"{fidelity}  ⟵ [via {ip.provided_name}] {ip.fidelity}"

        # The published/local manifest carries the chain-max tier, unioned
        # cannot_detect, and concatenated fidelity — so a discoverer of a PUBLISHED
        # chain reads the whole lineage's honesty from the record.
        manifest = {**lr.manifest, "consent_tier": eff,
                    "cannot_detect": cannot, "fidelity": fidelity}

        prov_inputs = []
        for m in mapping:
            if "inner_plan" in m:
                prov_inputs.append({"capability": m["hint"], "node_id": None,
                                    "provider_name": m["inner_plan"].provided_name,
                                    "derived_from": m["inner_plan"].provenance})
            else:
                p = m["provider"]
                prov_inputs.append({"capability": m["hint"],
                                    "node_id": p.get("node_id"),
                                    "provider_name": p.get("name")})

        provenance = Provenance(
            recipe=lr.recipe_name, version=lr.version, author_pubkey=lr.author_pubkey,
            inputs=prov_inputs, effective_tier=eff,
        )
        depth = 1 + max([ip.depth for ip in inners], default=0)
        return DerivationPlan(
            provided_name=lr.provided_name, recipe=lr, inputs=mapping,
            effective_tier=eff, manifest=manifest, provenance=provenance,
            fidelity=fidelity, cannot_detect=cannot,
            cost_rank_hint=lr.cost_rank_hint, num_inputs=len(mapping), depth=depth,
        )
