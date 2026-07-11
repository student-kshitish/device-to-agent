"""
thermal_ambient_proxy — transform.

Derives a COARSE ambient-temperature trend from a host's hottest thermal-zone
reading (sensing.thermal.max_temp_c). Deterministic, stdlib-only. All state lives
in `ctx` (no module globals), and no wall-clock / randomness is used, so two runs
over the same frames are byte-identical — the dry-run enforces this.

FIDELITY (stated honestly in recipe.json): this tracks the DIRECTION and rough
magnitude of change in a smoothed internal-temperature signal. It is NOT a
calibrated thermometer and cannot recover the absolute ambient temperature — the
device's own heat is an unknown offset.
"""

# Exponential smoothing weight for the new sample (deterministic).
_ALPHA = 0.3
# Change in the smoothed value (°C) that counts as a real trend, not noise.
_TREND_EPS = 0.5

_FIELD = "thermal.max_temp_c"


def init(ctx):
    ctx["ema"] = None        # smoothed proxy value
    ctx["prev_ema"] = None   # smoothed value at the previous frame
    ctx["trend"] = "steady"


def on_frame(input_name, frame, ctx):
    fields = frame.get("fields", {})
    if _FIELD not in fields:
        return None
    x = float(fields[_FIELD])

    prev = ctx["ema"]
    ema = x if prev is None else (_ALPHA * x + (1.0 - _ALPHA) * prev)
    ctx["prev_ema"] = prev
    ctx["ema"] = ema

    if prev is not None:
        d = ema - prev
        ctx["trend"] = "rising" if d > _TREND_EPS else "falling" if d < -_TREND_EPS else "steady"
    else:
        ctx["trend"] = "steady"

    return reading(ctx)


def reading(ctx):
    ema = ctx.get("ema")
    if ema is None:
        return None
    return {
        "ambient_trend_c": round(ema, 2),
        "trend": ctx.get("trend", "steady"),
        "confidence": "low",     # honest: this is a derived proxy, not a sensor
    }
