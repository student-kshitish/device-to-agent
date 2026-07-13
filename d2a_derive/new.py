"""
d2a_derive/new.py — scaffold a new recipe package.

    python -m d2a_derive.new <name> [dest_dir]

Authoring a recipe should make the format's CONSTITUTION unavoidable: a recipe must
be honest (declare its fidelity + blind spots) and must pass its own frames. Rather
than leave that to review, the scaffold bakes it into the starting point — every
mandatory honesty field is present but EMPTY, so the sign step (which refuses empty
honesty fields and a failing dry-run) will not let the author ship until they have
filled them in. The constitution is enforced at authoring time, not discovered at
review time.

What it writes (`<dest>/<name>/`):
  * recipe.json    — a template with every required envelope + provides key present.
                     author_pubkey/sig are empty (sign fills them); fidelity is ""
                     and cannot_detect is [] — DELIBERATELY, so sign refuses until the
                     author states the recipe's honest limits.
  * transform.py   — a runnable stub (init/on_frame/reading) that echoes one input
                     field, so once the honesty fields are filled the package passes
                     its own dry-run and can be signed.
  * test_frames.json — one normalized dry-run frame matching the stub.

The stub is intentionally minimal and correct: the ONLY thing standing between it
and a signature is the author writing an honest `fidelity` + `cannot_detect`.
"""

import argparse
import json
import sys
from pathlib import Path

from d2a_derive.recipe import RECIPE_JSON, TRANSFORM_PY, TEST_FRAMES_JSON


def scaffold_recipe(name: str) -> dict:
    """The template recipe.json dict. Honesty fields are present-but-empty by design
    (fidelity="" / cannot_detect=[]) so the sign step blocks until they are filled."""
    return {
        "name": name,
        "version": "0.1.0",
        "author_pubkey": "",                 # filled by `python -m d2a_derive.sign`
        "sig": "",                           # filled by sign
        "cost_rank_hint": 0,
        "unit_adaptations": {},
        "requires": [
            {
                "capability_hint": "compute",
                "fields": {
                    "cpu.util_pct": {"type": "number", "min_hz": 0.5}
                }
            }
        ],
        "provides": {
            "name": name,
            "derived": True,
            "recipe": name,
            # ↓↓↓ FILL THESE IN — sign refuses an empty fidelity / cannot_detect. ↓↓↓
            "fidelity": "",
            "cannot_detect": [],
            # ↑↑↑ state honestly what this substitute CAN and CANNOT do.          ↑↑↑
            "description": "TODO: what real capability does this substitute, and how?",
            "consent_tier": "open",
            "reading": {
                "value": {"type": "number", "description": "TODO: the derived reading"}
            },
            "streaming": False
        }
    }


_TRANSFORM_STUB = '''\
"""
transform.py — TODO: describe what this recipe computes and its honest limits.

Contract (deterministic, stdlib-only):
    init(ctx)                      — set up ctx (no wall-clock, no randomness)
    on_frame(input_name, frame, ctx) -> optional reading
    reading(ctx)                   -> the derived reading dict, or None

`frame` is a normalized {"input", "fields", "ts", "seq"} — read the recipe's
declared dotted fields out of frame["fields"]. Two runs over the same frames MUST
produce identical output (the dry-run enforces it).
"""


def init(ctx):
    ctx["value"] = 0.0


def on_frame(input_name, frame, ctx):
    fields = frame.get("fields", {})
    if "cpu.util_pct" in fields:
        # TODO: replace this passthrough with the real derivation.
        ctx["value"] = float(fields["cpu.util_pct"])
    return reading(ctx)


def reading(ctx):
    return {"value": round(ctx.get("value", 0.0), 2)}
'''

_TEST_FRAMES_STUB = [
    {"input": "compute", "fields": {"cpu.util_pct": 12.0}, "ts": 1.0, "seq": 1},
    {"input": "compute", "fields": {"cpu.util_pct": 37.0}, "ts": 2.0, "seq": 2},
]


def scaffold(name: str, dest_dir) -> Path:
    """Create `<dest_dir>/<name>/` with the three template files. Refuses to
    overwrite an existing directory. Returns the package directory."""
    pkg_dir = Path(dest_dir) / name
    if pkg_dir.exists():
        raise FileExistsError(f"{pkg_dir} already exists — refusing to overwrite")
    pkg_dir.mkdir(parents=True)

    (pkg_dir / RECIPE_JSON).write_text(
        json.dumps(scaffold_recipe(name), indent=2, sort_keys=True) + "\n")
    (pkg_dir / TRANSFORM_PY).write_text(_TRANSFORM_STUB)
    (pkg_dir / TEST_FRAMES_JSON).write_text(
        json.dumps(_TEST_FRAMES_STUB, indent=2) + "\n")
    return pkg_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m d2a_derive.new",
        description="Scaffold a new recipe package with the honesty fields required.")
    ap.add_argument("name", help="the recipe/package name")
    ap.add_argument("dest_dir", nargs="?", default=".",
                    help="directory to create the package under (default: cwd)")
    args = ap.parse_args(argv)

    try:
        pkg_dir = scaffold(args.name, args.dest_dir)
    except (FileExistsError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"scaffolded recipe package: {pkg_dir}")
    print("  next:")
    print(f"    1. edit {pkg_dir / TRANSFORM_PY} — implement the derivation")
    print(f"    2. edit {pkg_dir / RECIPE_JSON} — FILL fidelity + cannot_detect "
          "(sign refuses them empty)")
    print(f"    3. python -m d2a_derive.sign {pkg_dir} <your-keyname>")
    print(f"    4. python -m d2a_derive.conformance {args.name}   (after install)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
