"""
presence_from_activity — transform (presence-sensor SUBSTITUTE).

Derives a COARSE "machine-in-use" presence signal from a host's compute load
(cpu.util_pct + memory.used_percent). Deterministic, stdlib-only: all state lives
in `ctx` (an EMA of CPU utilization), no wall-clock, no randomness — two runs over
the same frames are identical, which the dry-run enforces.

FIDELITY (stated honestly in recipe.json): this infers that the MACHINE is being
used, NOT that a PERSON is present. A background job, a build, or a cron task move
the same needle as a human at the keyboard; an idle-but-occupied machine reads as
absent. It carries no identity and no notion of *who*.

CONSENT: presence inference is surveillance-adjacent, so recipe.json declares
consent_tier "sensitive" even though the compute inputs are open — the planner's
structural max() keeps the derived capability sensitive. This is the second
consent-escalation demonstration (open inputs -> sensitive derived).
"""

# Exponential smoothing weight for the new CPU sample (deterministic).
_ALPHA = 0.4
# Smoothed-utilization thresholds (percent) for the coarse activity bands.
_IN_USE_PCT = 15.0
_LIGHT_PCT  = 5.0

_CPU = "cpu.util_pct"
_MEM = "memory.used_percent"


def init(ctx):
    ctx["score"] = None      # smoothed cpu utilization
    ctx["mem"] = None        # last memory-used percent (context only)


def on_frame(input_name, frame, ctx):
    fields = frame.get("fields", {})
    if _CPU in fields:
        u = float(fields[_CPU])
        prev = ctx["score"]
        ctx["score"] = u if prev is None else (_ALPHA * u + (1.0 - _ALPHA) * prev)
    if _MEM in fields:
        ctx["mem"] = float(fields[_MEM])
    return reading(ctx)


def reading(ctx):
    s = ctx.get("score")
    if s is None:
        return None
    if s >= _IN_USE_PCT:
        level = "active"
    elif s >= _LIGHT_PCT:
        level = "light"
    else:
        level = "idle"
    return {
        "in_use":         bool(s >= _IN_USE_PCT),
        "activity_level": level,
        "score":          round(s, 2),
    }
