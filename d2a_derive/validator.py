"""
d2a_derive/validator.py — recipe schema, provides-manifest, and requires contract.

Three jobs, each returning distinct, informative failures:

  1. validate_recipe_schema(recipe)   — the recipe.json envelope is well-formed.
  2. validate_provides(provides)       — the derived capability's manifest is a
     LEGAL manifest (REUSES d2a.manifest.validate_manifest verbatim), plus the
     mandatory derivation-metadata keys are well-formed.
  3. check_input_against_provider(...) — a discovered provider's manifest
     satisfies ONE of the recipe's `requires` inputs (fields, types, units incl.
     declared adaptations, and min_hz vs the known cadence clamp).

MANIFEST REUSE (the key design point). A recipe's `provides` is a normal manifest
PLUS a `name` (the capability's identity). As of v1.5 the derivation-provenance
keys (derived/recipe/fidelity/cannot_detect) are MANIFEST VOCABULARY, validated by
d2a.manifest.validate_manifest itself — so we pop ONLY `name` (a manifest has no
name field) and hand the rest to validate_manifest with expected_consent_tier ==
the manifest's OWN consent_tier. The Phase-1 workaround that popped all five
metadata keys (because the validator rejected unknown top-level keys) is REMOVED.
The real consent decision — effective = max(input tiers, declared output tier) —
is applied later by the planner, NOT here.

min_hz / CADENCE (v1.5 — protocol gap 1 CLOSED): manifests may now declare a
per-field native `hz`. When a provider field declares it, the contract check
compares min_hz against that real cadence; when it is absent, we fall back to the
device-side clamp DERIVE_MAX_INPUT_HZ (mirroring runtimes.device_runtime.
MAX_SAMPLE_HZ, 10.0 Hz) — an agent can never receive frames faster than the device
streams them. See check_input_against_provider.
"""

from d2a import manifest as _manifest
from d2a.manifest import ManifestError
from d2a_derive import errors
from d2a_derive import units

# Mirrors runtimes.device_runtime.MAX_SAMPLE_HZ. NOT imported (an agent-side engine
# must not pull in the device runtime); duplicated as a known protocol constant.
DERIVE_MAX_INPUT_HZ = 10.0

# The derivation-metadata field names carried inside `provides`. As of v1.5 four of
# them (derived/recipe/fidelity/cannot_detect) are MANIFEST VOCABULARY validated by
# d2a.manifest.validate_manifest; only `name` is non-manifest and popped before
# validation (see validate_provides). Kept for reference / back-compat export.
DERIVE_META_KEYS = frozenset({"name", "derived", "recipe", "fidelity", "cannot_detect"})

# Recipe.json required top-level envelope keys.
_REQUIRED_TOP = ("name", "version", "author_pubkey", "sig",
                 "requires", "provides", "unit_adaptations", "cost_rank_hint")

_JSON_TYPE_TO_PY = {
    "number":  (int, float),
    "string":  str,
    "boolean": bool,
    "object":  dict,
    "array":   list,
}


def _fail(detail: str) -> "errors.DeriveError":
    return errors.DeriveError(errors.RECIPE_INVALID, detail)


# ── 1. recipe envelope ────────────────────────────────────────────────────────

def validate_recipe_schema(recipe: dict) -> None:
    """Validate the recipe.json envelope (NOT the signature — that is the trust
    gate, run earlier). Raises DeriveError(RECIPE_INVALID) with a path-naming
    message on any violation."""
    if not isinstance(recipe, dict):
        raise _fail("recipe must be a JSON object")
    missing = [k for k in _REQUIRED_TOP if k not in recipe]
    if missing:
        raise _fail(f"missing required keys {missing}")

    for k in ("name", "version", "author_pubkey", "sig"):
        if not isinstance(recipe[k], str) or not recipe[k]:
            raise _fail(f"'{k}' must be a non-empty string")

    if not isinstance(recipe["cost_rank_hint"], int) or isinstance(recipe["cost_rank_hint"], bool):
        raise _fail("'cost_rank_hint' must be an integer")

    ua = recipe["unit_adaptations"]
    if not isinstance(ua, dict) or not all(
            isinstance(a, str) and isinstance(b, str) for a, b in ua.items()):
        raise _fail("'unit_adaptations' must be an object of str->str")

    deps = recipe.get("deps", [])
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise _fail("'deps' must be a list of strings")

    _validate_requires(recipe["requires"])
    # provides is validated separately (validate_provides) so the "bad vocabulary
    # inside provides" failure is a distinct, testable step.


def _validate_requires(requires) -> None:
    if not isinstance(requires, list) or not requires:
        raise _fail("'requires' must be a non-empty list")
    for i, inp in enumerate(requires):
        where = f"requires[{i}]"
        if not isinstance(inp, dict):
            raise _fail(f"{where}: must be an object")
        hint = inp.get("capability_hint")
        if hint is not None and not isinstance(hint, str):
            raise _fail(f"{where}.capability_hint: must be a string")
        fields = inp.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise _fail(f"{where}.fields: must be a non-empty object")
        for fname, spec in fields.items():
            fw = f"{where}.fields.{fname}"
            if not isinstance(spec, dict):
                raise _fail(f"{fw}: must be an object")
            t = spec.get("type")
            if t not in _JSON_TYPE_TO_PY:
                raise _fail(f"{fw}.type: must be one of {sorted(_JSON_TYPE_TO_PY)}, got {t!r}")
            if "unit" in spec and not isinstance(spec["unit"], str):
                raise _fail(f"{fw}.unit: must be a string")
            if "min_hz" in spec:
                mh = spec["min_hz"]
                if not isinstance(mh, (int, float)) or isinstance(mh, bool) or mh <= 0:
                    raise _fail(f"{fw}.min_hz: must be a positive number")


# ── 2. provides manifest + derivation metadata ────────────────────────────────

def validate_provides(provides: dict) -> tuple[dict, dict]:
    """
    Validate the recipe's `provides`. Returns (validated_manifest, meta) where:
      - validated_manifest is the manifest-vocabulary subset run through
        d2a.manifest.validate_manifest (streaming defaulted, size-capped).
      - meta = {name, derived, recipe, fidelity, cannot_detect}.

    Raises DeriveError(RECIPE_INVALID) on any manifest vocabulary violation or
    malformed metadata. This runs at REGISTRY LOAD, so a recipe with bad
    vocabulary inside provides is refused before it can ever be a plan candidate.
    """
    if not isinstance(provides, dict):
        raise _fail("'provides' must be an object")

    # v1.5: the derived-provenance keys (derived/recipe/fidelity/cannot_detect) are
    # now MANIFEST VOCABULARY, validated by d2a.manifest.validate_manifest itself.
    # Phase 1 POPPED all five metadata keys before validation because the manifest
    # validator rejected unknown top-level keys; that workaround is REMOVED here —
    # only 'name' (the capability's identity, not manifest content) is still
    # separated out, because a manifest has no 'name' field.
    name = provides.get("name")
    if not isinstance(name, str) or not name:
        raise _fail("provides.name is required and must be a non-empty string")

    manifest_part = {k: v for k, v in provides.items() if k != "name"}

    # A recipe always PROVIDES a derived capability. Enforce it at the derive layer
    # so a recipe that forgot 'derived' fails with a derive-level message (the
    # manifest validator would merely allow a non-derived manifest through).
    if manifest_part.get("derived") is not True:
        raise _fail("provides.derived must be true for a recipe's output")

    tier = manifest_part.get("consent_tier")
    if tier not in ("open", "sensitive"):
        raise _fail(f"provides.consent_tier must be 'open' or 'sensitive', got {tier!r}")

    # REUSE d2a's manifest validator verbatim; it now enforces the derived-key
    # contract (recipe/fidelity/cannot_detect required, well-formed). expected tier
    # == the manifest's own (self-consistent — the real consent escalation is the
    # planner's max()).
    try:
        validated = _manifest.validate_manifest(manifest_part, expected_consent_tier=tier)
    except ManifestError as exc:
        raise _fail(f"provides manifest invalid: {exc}") from exc

    meta = {
        "name":          name,
        "derived":       validated["derived"],
        "recipe":        validated["recipe"],
        "fidelity":      validated["fidelity"],
        "cannot_detect": validated["cannot_detect"],
    }
    return validated, meta


# ── 3. requires contract check (recipe input vs a discovered provider) ────────

def check_input_against_provider(req_input: dict, provider_manifest: dict,
                                 unit_adaptations: dict) -> tuple[bool, str]:
    """
    Does `provider_manifest` satisfy `req_input` (one entry of a recipe's
    `requires`)? Returns (ok, reason). Checks, per required field:
      - presence in the provider manifest's reading
      - exact type match
      - unit match: equal, OR (provider_unit -> required_unit) is BOTH declared in
        the recipe's unit_adaptations AND a scale the engine actually knows
      - min_hz <= DERIVE_MAX_INPUT_HZ (the coarse cadence gate; see module docs)
    """
    reading = provider_manifest.get("reading", {})
    if not isinstance(reading, dict):
        return False, "provider manifest has no reading schema"

    for fname, spec in req_input.get("fields", {}).items():
        pf = reading.get(fname)
        if pf is None:
            return False, f"missing field '{fname}'"

        if pf.get("type") != spec.get("type"):
            return False, (f"field '{fname}' type mismatch: recipe wants "
                           f"{spec.get('type')!r}, provider has {pf.get('type')!r}")

        req_unit = spec.get("unit")
        if req_unit is not None:
            prov_unit = pf.get("unit")
            if prov_unit != req_unit:
                declared = unit_adaptations.get(prov_unit) == req_unit
                if not (declared and units.can_adapt(prov_unit or "", req_unit)):
                    return False, (f"field '{fname}' unit mismatch: recipe wants "
                                   f"{req_unit!r}, provider has {prov_unit!r} "
                                   f"(no declared+supported adaptation)")

        min_hz = spec.get("min_hz")
        if min_hz is not None:
            # v1.5: if the provider declares its native per-field cadence (hz), the
            # contract check uses THAT — the real cadence fact. Only when hz is
            # absent do we fall back to the coarse device MAX_SAMPLE_HZ clamp (the
            # Phase-1 behaviour, which is all we could do before the manifest carried
            # cadence). This closes derivation protocol gap 1.
            prov_hz = pf.get("hz")
            if isinstance(prov_hz, (int, float)) and not isinstance(prov_hz, bool):
                if min_hz > prov_hz:
                    return False, (f"field '{fname}' needs min_hz={min_hz}, but the "
                                   f"provider declares native hz={prov_hz} (too slow)")
            elif min_hz > DERIVE_MAX_INPUT_HZ:
                return False, (f"field '{fname}' needs min_hz={min_hz}, provider "
                               f"declares no hz, and the device cadence clamp is "
                               f"{DERIVE_MAX_INPUT_HZ} Hz (unsatisfiable)")

    return True, "ok"
