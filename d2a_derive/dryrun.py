"""
d2a_derive/dryrun.py — the DRY-RUN admission gate.

A recipe is executed against its OWN test_frames.json before it is ever admitted
to the registry, so a recipe that fails its own frames can NEVER bind hardware.
The dry-run enforces two properties:

  1. VALIDITY — feeding the fixtures through the transform yields a final reading
     that VALIDATES against the recipe's provides manifest (exactly the declared
     reading fields, each of the declared type).

  2. DETERMINISM — running the fixtures twice from a fresh ctx yields IDENTICAL
     output. This catches hidden state (globals), wall-clock reads, and randomness
     BEFORE the recipe touches real inputs. Deterministic + stdlib-only is the
     reference-recipe contract; this is where it is mechanically enforced.

NORMALIZED INPUT FRAME (what the transform's on_frame receives — the same shape
Phase 2's live executor will build from a real get_reading frame):
    {"input": <requires-input name/hint>, "fields": {<field>: <value>, ...},
     "ts": <number>, "seq": <int>}
Keeping test_frames.json in this normalized shape means the dry-run exercises the
exact call path the live feed will.
"""

from dataclasses import dataclass, field

from d2a_derive import errors
from d2a_derive.recipe import RecipePackage
from d2a_derive.validator import _JSON_TYPE_TO_PY


@dataclass
class DryRunResult:
    ok: bool
    reason: str = ""
    sample_output: dict = field(default_factory=dict)


def _validate_output(output, manifest: dict) -> tuple[bool, str]:
    """The transform's final reading must be exactly the manifest's declared
    reading fields, each of the declared type."""
    if not isinstance(output, dict):
        return False, "transform reading() did not return an object"
    declared = manifest.get("reading", {})
    got, want = set(output), set(declared)
    if got != want:
        missing, extra = want - got, got - want
        return False, (f"output fields {sorted(got)} != declared {sorted(want)} "
                       f"(missing={sorted(missing)}, extra={sorted(extra)})")
    for fname, spec in declared.items():
        py = _JSON_TYPE_TO_PY[spec["type"]]
        val = output[fname]
        # bool is a subclass of int — exclude it from "number" unless declared boolean
        if spec["type"] == "number" and isinstance(val, bool):
            return False, f"field '{fname}' is a boolean, expected number"
        if not isinstance(val, py):
            return False, f"field '{fname}' has wrong type: expected {spec['type']}"
    return True, "ok"


def _run_once(module, frames: list) -> dict:
    """Feed frames through a FRESH ctx; return the final reading(ctx)."""
    ctx: dict = {}
    module.init(ctx)
    for fr in frames:
        module.on_frame(fr.get("input", ""), fr, ctx)
    out = module.reading(ctx)
    return out


def dry_run(pkg: RecipePackage, module, validated_manifest: dict) -> DryRunResult:
    """
    Run the recipe's transform against test_frames.json twice (fresh ctx each
    time), enforce determinism, and validate the final reading against the
    provides manifest. Returns a DryRunResult; never raises for a recipe fault
    (that is reported via ok=False), only for a genuinely broken package.
    """
    try:
        frames = pkg.load_test_frames()
    except Exception as exc:                     # noqa: BLE001
        return DryRunResult(False, f"could not load test_frames.json: {exc}")
    if not frames:
        return DryRunResult(False, "test_frames.json is empty — nothing to dry-run")

    try:
        out1 = _run_once(module, frames)
        out2 = _run_once(module, frames)
    except Exception as exc:                     # noqa: BLE001 — author code raised
        return DryRunResult(False, f"transform raised during dry-run: {exc}")

    if out1 != out2:
        return DryRunResult(False,
                            "non-deterministic: two runs over the same frames differ "
                            "(hidden state, wall-clock, or randomness)")

    ok, reason = _validate_output(out1, validated_manifest)
    if not ok:
        return DryRunResult(False, f"dry-run output does not validate: {reason}",
                            sample_output=out1 if isinstance(out1, dict) else {})

    return DryRunResult(True, "ok", sample_output=out1)
