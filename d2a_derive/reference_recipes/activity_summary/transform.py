"""
activity_summary — transform (a SECOND-HOP recipe: derives from a derived input).

Consumes a `presence` signal (itself derived by presence_from_activity) and rolls
it up into a coarse DUTY-CYCLE / occupancy-pattern summary. This is the top of a
two-hop chain: compute → presence → activity_summary. Deterministic, stdlib-only —
state is two integer counters in `ctx`, no wall-clock, no randomness, so two runs
over the same frames are identical.

FIDELITY (recipe.json): a duty cycle over sampled presence. It INHERITS every limit
of presence_from_activity (it cannot see a person, only machine activity) and ADDS
sampling-window coarseness on top — the chain's cannot_detect is the UNION.

CONSENT: an occupancy PATTERN over time is at least as sensitive as instantaneous
presence. The recipe declares `open`, but the chain-max consent rule keeps the
derived capability SENSITIVE (its presence input is sensitive) — the chained
consent-escalation demonstration.
"""

# Duty-cycle thresholds for the coarse occupancy pattern.
_STEADY = 0.66
_INTERMITTENT = 0.15

_IN_USE = "in_use"


def init(ctx):
    ctx["total"] = 0
    ctx["in_use"] = 0


def on_frame(input_name, frame, ctx):
    fields = frame.get("fields", {})
    if _IN_USE in fields:
        ctx["total"] += 1
        if bool(fields[_IN_USE]):
            ctx["in_use"] += 1
    return reading(ctx)


def reading(ctx):
    total = ctx.get("total", 0)
    if total == 0:
        return None
    duty = ctx["in_use"] / total
    if duty >= _STEADY:
        pattern = "steady"
    elif duty >= _INTERMITTENT:
        pattern = "intermittent"
    else:
        pattern = "idle"
    return {
        "duty_cycle": round(duty, 3),
        "samples":    total,
        "pattern":    pattern,
    }
