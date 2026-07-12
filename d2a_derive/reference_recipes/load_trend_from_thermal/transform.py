"""
load_trend_from_thermal — transform (power/load-meter SUBSTITUTE).

Derives a crude SUSTAINED-LOAD trend from a host's hottest thermal zone
(thermal.max_temp_c) combined with CPU utilization (cpu.util_pct). Deterministic,
stdlib-only: state is an EMA of temperature plus the last utilization sample in
`ctx`, no wall-clock, no randomness — two runs over the same frames are identical.

FIDELITY (stated honestly in recipe.json): this is NOT a calibrated power meter. It
correlates rising thermals under compute load into a coarse low/moderate/high band,
but it is CONFOUNDED by ambient temperature changes, fan curves, and heat from
other components (GPU, disk). Treat it as a trend hint, never a wattage.

CONSENT: open — a load trend is not sensitive (unlike presence). Inputs are open,
declared output open, effective tier open.
"""

# Exponential smoothing weight for the new temperature sample (deterministic).
_ALPHA = 0.3
# Change in smoothed temp (deg C) that counts as a real trend, not noise.
_TREND_EPS = 0.5
# Bands: "hot" smoothed temp and "busy" CPU utilization.
_HOT_C     = 60.0
_BUSY_PCT  = 50.0

_TEMP = "thermal.max_temp_c"
_CPU  = "cpu.util_pct"


def init(ctx):
    ctx["ema"] = None        # smoothed temperature
    ctx["prev_ema"] = None
    ctx["util"] = None       # last cpu utilization


def on_frame(input_name, frame, ctx):
    fields = frame.get("fields", {})
    if _TEMP in fields:
        t = float(fields[_TEMP])
        prev = ctx["ema"]
        ctx["prev_ema"] = prev
        ctx["ema"] = t if prev is None else (_ALPHA * t + (1.0 - _ALPHA) * prev)
    if _CPU in fields:
        ctx["util"] = float(fields[_CPU])
    return reading(ctx)


def reading(ctx):
    ema = ctx.get("ema")
    if ema is None:
        return None

    prev = ctx.get("prev_ema")
    if prev is None:
        trend = "steady"
    else:
        d = ema - prev
        trend = "rising" if d > _TREND_EPS else "falling" if d < -_TREND_EPS else "steady"

    util = ctx.get("util")
    hot  = ema >= _HOT_C
    busy = util is not None and util >= _BUSY_PCT
    if hot and busy:
        load = "high"
    elif hot or busy:
        load = "moderate"
    else:
        load = "low"

    return {
        "sustained_load":  load,
        "trend":           trend,
        "smoothed_temp_c": round(ema, 2),
    }
