"""
d2a_derive/ — CASE 4: CAPABILITY DERIVATION (application layer).

WHAT THIS IS
    When an agent needs capability X and no device provides it, derive a
    FUNCTIONAL SUBSTITUTE from capabilities that DO exist, using community-grade
    RECIPE PACKAGES. A recipe reads real device fields and computes a coarser,
    honestly-labelled stand-in (e.g. a free-space map inferred from a motion
    trajectory; an ambient-temperature trend proxied from thermal-zone maxima).

WHERE IT SITS (and what it is NOT)
    This is a pure APPLICATION LAYER built ON TOP OF the agent surface. It adds
    NO wire verbs and makes NO protocol changes. It drives an ordinary
    RemoteAgent (Phase 2) and reuses d2a's manifest validator + Ed25519 crypto
    verbatim. Protocol gaps it exposes are REPORTED (see README + docstrings),
    never patched here. It lives as a TOP-LEVEL package — not under d2a/ (which
    stays protocol-only) and not under agents/ (it is not "one more agent") — so
    the "application layer, no protocol changes" boundary is visible in the tree,
    and so community-contributed recipes have an obvious, self-contained home.

RECIPE PACKAGE (a directory, community-grade: signed + self-contained)
    <name>/recipe.json + transform.py + test_frames.json
    - recipe.json is Ed25519-SIGNED over its canonical JSON (author_pubkey inside
      the signed bytes; "sig" outside). See recipe.py / sign.py.
    - transform.py is deterministic, stdlib-only Python: init(ctx),
      on_frame(input_name, frame, ctx) -> optional output, reading(ctx).
    - test_frames.json drives the DRY-RUN: a recipe that fails its own frames (or
      is non-deterministic across two runs) is never admitted to the registry,
      so it can never bind hardware.

TRUST v1 (honesty statement — mirrored in trust.py, loader.py, README)
    A recipe loads ONLY if (a) its signature verifies against its embedded
    author_pubkey AND (b) that pubkey is in the user's trusted_authors.json
    (explicit review-then-trust install step). LOADING transform.py IS EXECUTING
    it, and calling on_frame runs recipe-author code IN-PROCESS, UNSANDBOXED. The
    signature therefore proves AUTHORSHIP, NOT SAFETY. The trust gate runs
    STRICTLY BEFORE importlib ever touches transform.py, so untrusted code is
    never imported — but a trusted author's bug or malice is not caught here.

CONSENT (structural, non-overridable)
    effective tier = max(all input capability tiers, recipe's declared output
    tier). The derived capability is registered locally at that tier. Mapping is
    sensitive REGARDLESS of input tiers — trajectory_free_space_map is the
    consent-escalation demonstration (open inputs -> sensitive derived).

PHASE STATUS
    Phase 1 (this commit): recipe format, signing helper, trust store, validator
    (schema + provides-manifest + requires contract-check), registry (with
    dry-run admission), planner (need(): direct-first -> match -> contract ->
    cost-rank -> dry-run gate -> DerivationPlan), provenance + effective tier.
    NO hardware is bound in Phase 1 — need() returns a PLAN, not a live object.

    Phase 2 (next): the live executor (bind inputs via RemoteAgent, feed the
    transform via on_event), the DerivedCapability object with a live .reading()
    / .state / .health() / .close(), self-healing, the staleness monitor, and the
    runnable demo (incl. the demo_odometry Phase-2 scaffolding source).
"""

from d2a_derive import errors
from d2a_derive.recipe import RecipePackage, sign_recipe, verify_recipe_sig, recipe_signing_bytes
from d2a_derive.trust import TrustStore
from d2a_derive.validator import (
    validate_recipe_schema, validate_provides, check_input_against_provider,
    DERIVE_MAX_INPUT_HZ, DERIVE_META_KEYS,
)
from d2a_derive.dryrun import dry_run, DryRunResult
from d2a_derive.registry import Registry, LoadedRecipe, RecipeError
from d2a_derive.planner import Planner, NeedResult, DerivationPlan, Provenance, effective_tier
from d2a_derive.executor import DerivedCapability, InputFeed
from d2a_derive.healer import SelfHealer
from d2a_derive.monitor import StalenessMonitor

__all__ = [
    "errors",
    "RecipePackage", "sign_recipe", "verify_recipe_sig", "recipe_signing_bytes",
    "TrustStore",
    "validate_recipe_schema", "validate_provides", "check_input_against_provider",
    "DERIVE_MAX_INPUT_HZ", "DERIVE_META_KEYS",
    "dry_run", "DryRunResult",
    "Registry", "LoadedRecipe", "RecipeError",
    "Planner", "NeedResult", "DerivationPlan", "Provenance", "effective_tier",
    # Phase 2 — live derivation
    "DerivedCapability", "InputFeed", "SelfHealer", "StalenessMonitor",
]
