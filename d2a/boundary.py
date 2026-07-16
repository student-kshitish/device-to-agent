"""
d2a/boundary.py — PHASE 11: capability boundaries (the MCP "roots" concept,
adapted): a declared, device-enforced OPERATIONAL LANE for a capability.

A capability's manifest may carry an optional "boundary" key — the set of
targets/params it may EVER act on — checked by the device BEFORE the consent
gate, so an out-of-boundary request is refused structurally regardless of any
approval. Boundary is a structural pre-filter, NOT a replacement for consent:
in-boundary still requires the full owner approval. Both must hold.

LEAF MODULE: pure stdlib (fnmatch for glob), no d2a imports — mirrors
d2a/conditions.py so both the manifest validator and the device verb layer
import it cycle-free.

VOCABULARY (small and fixed, like manifests/conditions — deliberately NO
expression language):

    "boundary": { <key>: <constraint>, ... }

  <key>        = "target" (the capability's fixed attach-time target)
                 | a param name declared by some action in the manifest
  <constraint> = {"in":    [<scalar>, ...]}   exact set — string or number
               | {"match": "<glob>"}          fnmatch glob — string only
               | {"range": [<min>, <max>]}    inclusive numeric range

Exactly ONE match-type key per constraint. Validated at publish time against
the manifest (a boundary on a param no action takes is rejected); enforced at
propose time against {"target": <fixed target>, **plan_params}.

DENY-BY-DEFAULT SHAPE: if a boundary names a param and the plan OMITS it, that
is a violation — a constrained param is effectively required. (Otherwise an
agent could dodge e.g. {"signal": {"in": ["TERM"]}} by omitting `signal` and
riding the executor's default.) An ABSENT boundary changes nothing (compat).

GENERIC: check() takes an opaque flat values dict, so later adopters reuse it
unchanged — diagnostics ({"target": <node>}), derivation ({"capability": <name>}).
v1.11 ships enforcement for the intervention tier only; the manifest validator
REJECTS "boundary" on other tiers (a declared boundary nobody enforces would
look like protection).
"""

from fnmatch import fnmatchcase

# The reserved key naming the capability's fixed attach-time target (a systemd
# unit / device node / module). Always type "string" for constraint purposes.
TARGET_KEY = "target"

MATCH_TYPES = {"in", "match", "range"}


class BoundaryError(ValueError):
    """Raised when a boundary violates the fixed vocabulary or does not match
    the capability manifest. The message names the exact offending key."""


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _param_types(manifest: dict) -> dict:
    """{param_name: manifest type} across ALL actions. A param appearing in two
    actions with different types maps to the FIRST seen — constraints are then
    type-checked against that; manifests this codebase ships never conflict."""
    out: dict = {}
    for aspec in (manifest.get("actions") or {}).values():
        for pname, pspec in (aspec.get("params") or {}).items():
            out.setdefault(pname, (pspec or {}).get("type"))
    return out


def validate_boundary(boundary: dict, manifest: dict) -> dict:
    """
    Validate `boundary` against `manifest` (the capability's self-description).
    Returns the boundary on success; raises BoundaryError otherwise.

    Checks, in order: shape, non-empty, key exists ("target" or a declared
    action param), exactly one match type, constraint/param type compatibility.
    """
    if not isinstance(boundary, dict):
        raise BoundaryError("boundary must be an object")
    if not boundary:
        raise BoundaryError("boundary must not be empty (declared-but-vacuous "
                            "would look like protection)")

    ptypes = _param_types(manifest if isinstance(manifest, dict) else {})

    for key, constraint in boundary.items():
        where = f"boundary.{key}"
        if key == TARGET_KEY:
            ktype = "string"
        elif key in ptypes:
            ktype = ptypes[key]
        else:
            raise BoundaryError(
                f"{where}: no action takes a param {key!r} "
                f"(known: {sorted(ptypes) + [TARGET_KEY]})")

        if not isinstance(constraint, dict):
            raise BoundaryError(f"{where}: constraint must be an object")
        kinds = set(constraint) & MATCH_TYPES
        if set(constraint) - MATCH_TYPES:
            raise BoundaryError(
                f"{where}: unknown keys {sorted(set(constraint) - MATCH_TYPES)}")
        if len(kinds) != 1:
            raise BoundaryError(
                f"{where}: exactly one of {sorted(MATCH_TYPES)} is required, "
                f"got {sorted(kinds)}")
        kind = kinds.pop()
        val = constraint[kind]

        if kind == "in":
            if not isinstance(val, list) or not val:
                raise BoundaryError(f"{where}: 'in' must be a non-empty list")
            for item in val:
                if ktype == "number" and not _is_number(item):
                    raise BoundaryError(
                        f"{where}: 'in' items must be numbers for a number param")
                if ktype == "string" and not isinstance(item, str):
                    raise BoundaryError(
                        f"{where}: 'in' items must be strings for a string param")
                if ktype not in ("number", "string"):
                    raise BoundaryError(
                        f"{where}: 'in' requires a string or number param, "
                        f"but {key!r} is {ktype!r}")
        elif kind == "match":
            if ktype != "string":
                raise BoundaryError(
                    f"{where}: 'match' requires a string param, but {key!r} is {ktype!r}")
            if not isinstance(val, str) or not val:
                raise BoundaryError(f"{where}: 'match' must be a non-empty glob string")
        else:  # range
            if ktype != "number":
                raise BoundaryError(
                    f"{where}: 'range' requires a number param, but {key!r} is {ktype!r}")
            if (not isinstance(val, list) or len(val) != 2
                    or not all(_is_number(x) for x in val) or val[0] > val[1]):
                raise BoundaryError(
                    f"{where}: 'range' must be [min, max] with numeric min <= max")

    return boundary


def check(boundary: dict | None, values: dict) -> tuple[bool, str]:
    """
    Enforce a VALIDATED boundary against a flat values dict — for interventions
    that is {"target": <fixed attach-time target>, **plan_params}. Returns
    (ok, why); `why` names the first violated key + constraint so the refusal
    (and its audit entry) is self-explanatory.

    Absent/empty boundary → (True, "") — compat, behavior unchanged. A boundary
    key ABSENT from `values` is a violation (a constrained param is effectively
    required — see module docstring).
    """
    if not boundary:
        return True, ""
    for key, constraint in boundary.items():
        if key not in values:
            return False, (f"boundary constrains {key!r} but the plan does not "
                           f"supply it (a constrained param is required)")
        v = values[key]
        if "in" in constraint:
            if v not in constraint["in"]:
                return False, f"{key}={v!r} not in allowed set {constraint['in']}"
        elif "match" in constraint:
            if not isinstance(v, str) or not fnmatchcase(v, constraint["match"]):
                return False, f"{key}={v!r} does not match {constraint['match']!r}"
        elif "range" in constraint:
            lo, hi = constraint["range"]
            if not _is_number(v) or not (lo <= v <= hi):
                return False, f"{key}={v!r} outside range [{lo}, {hi}]"
    return True, ""
