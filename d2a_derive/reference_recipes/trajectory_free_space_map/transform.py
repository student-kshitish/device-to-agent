"""
trajectory_free_space_map — transform.

Derives a SPARSE free-space occupancy map from a device's motion trajectory: every
grid cell the device physically passed through is marked traversable. Deterministic,
stdlib-only. State lives in `ctx`; the outputs are pure counts/areas independent of
set iteration order, so two runs over the same frames are identical.

FIDELITY (stated honestly in recipe.json): this marks ONLY visited cells as free.
It says NOTHING about unvisited cells and CANNOT detect obstacles, walls, or dynamic
objects — absence of a cell is absence of evidence, not evidence of an obstacle.

CONSENT: mapping a space is SENSITIVE regardless of how open the positional inputs
are. recipe.json declares consent_tier "sensitive"; with open inputs the planner's
structural max() still yields an effective SENSITIVE tier — the escalation demo.
"""

# Grid resolution in metres. Deterministic constant (no config, no randomness).
_GRID_RES_M = 0.5

_FX = "pose.x_m"
_FY = "pose.y_m"


def init(ctx):
    ctx["cells"] = set()     # set of (ix, iy) grid indices visited
    ctx["res"] = _GRID_RES_M


def on_frame(input_name, frame, ctx):
    fields = frame.get("fields", {})
    if _FX not in fields or _FY not in fields:
        return None
    res = ctx["res"]
    ix = int(round(float(fields[_FX]) / res))
    iy = int(round(float(fields[_FY]) / res))
    ctx["cells"].add((ix, iy))
    return reading(ctx)


def reading(ctx):
    cells = ctx.get("cells")
    if not cells:
        return None
    res = ctx["res"]
    n = len(cells)
    return {
        "free_cells": n,
        "grid_res_m": res,
        "coverage_m2": round(n * res * res, 4),
    }
