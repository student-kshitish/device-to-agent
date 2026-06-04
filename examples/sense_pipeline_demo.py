"""
sense_pipeline_demo.py — D2A Sense Layer Part 1 demo.

Creates a DeviceRuntime (no network needed), exercises the full pipeline in all
four shapes, then burns CPU in multiple processes to push utilization and shows
the verdict shifting comfort → caution with rising deltas and rates — proving
the pipeline is trend-aware and fresh.
"""

import multiprocessing
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime

SEP  = "=" * 70
SEP2 = "-" * 70


# ── CPU burner (separate processes to bypass GIL and create real load) ────────

def _burn_worker(duration_secs: float) -> None:
    """Pure spin for duration_secs seconds — no IPC, no GIL contention."""
    import time as _time
    end = _time.time() + duration_secs
    x = 0
    while _time.time() < end:
        x += x + 1  # actual arithmetic so the loop isn't optimised away


def start_cpu_burn(seconds: float = 14.0, num_workers: int | None = None) -> list:
    """Start num_workers processes that each spin for `seconds`. Returns process list."""
    if num_workers is None:
        num_workers = max(2, os.cpu_count() or 4)
    procs = [
        multiprocessing.Process(target=_burn_worker, args=(seconds,), daemon=True)
        for _ in range(num_workers)
    ]
    for p in procs:
        p.start()
    return procs


def stop_cpu_burn(procs: list) -> None:
    for p in procs:
        if p.is_alive():
            p.terminate()
        p.join(timeout=1)


# ── helpers ───────────────────────────────────────────────────────────────────

def fv(features: dict, name: str) -> float:
    """Look up a named feature value from the vector."""
    try:
        idx = features["names"].index(name)
        return features["vector"][idx]
    except (ValueError, IndexError):
        return float("nan")


# ── start runtime ─────────────────────────────────────────────────────────────

print(SEP)
print("SENSE LAYER PART 1 — forward pipeline demo")
print(SEP)

runtime = DeviceRuntime(name="sense-demo")
print()

# ── warm up CPUSource (first /proc/stat read seeds the diff) ──────────────────
runtime.sense_reading("compute", shape="raw")
time.sleep(0.2)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — ALL FOUR SHAPES for 'compute'
# ─────────────────────────────────────────────────────────────────────────────

print(SEP)
print("SECTION 1 — all four shapes for 'compute'")
print(SEP)

for shape in ("raw", "normalized", "features", "verdict"):
    time.sleep(0.15)
    frame = runtime.sense_reading("compute", shape=shape)

    print()
    print(f"[shape={shape}]  seq={frame.seq}  verdict={frame.verdict}  "
          f"advice={frame.advice}  confidence={frame.confidence:.3f}")

    if shape == "raw":
        raw_data = frame.data or {}
        for src, vals in sorted(raw_data.items()):
            print(f"  {src}: {vals}")

    elif shape == "normalized":
        norm_data = frame.data or {}
        for src, vals in sorted(norm_data.items()):
            print(f"  {src}: {vals}")

    elif shape == "features":
        feat = frame.data or {}
        names = feat.get("names", [])
        vec   = feat.get("vector", [])
        print(f"  suggested_processing : {feat.get('suggested_processing')}")
        print(f"  feature_count        : {len(names)}")
        # Show the first 12 feature slots (4 fields × 3 entries each)
        for i in range(min(12, len(names))):
            print(f"    [{i:2d}]  {names[i]:<45s}  {vec[i]:+.6f}")

    elif shape == "verdict":
        print(f"  data : {frame.data}")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — CPU burn + verdict shift + trend-awareness proof
# ─────────────────────────────────────────────────────────────────────────────

print()
print(SEP)
print("SECTION 2 — CPU burn: watching verdict shift and delta/rate change")
print(SEP)
print()

# Two warm-up reads so the feature extractor has a previous sample to diff against.
runtime.sense_reading("compute", shape="features")
time.sleep(0.25)
runtime.sense_reading("compute", shape="features")
time.sleep(0.25)

# Start CPU burn across all cores.
n_workers = max(2, os.cpu_count() or 4)
print(f"  Starting {n_workers} CPU-burn processes (one per logical core) …")
burn_procs = start_cpu_burn(seconds=14.0, num_workers=n_workers)
time.sleep(0.6)   # let the kernel see the load before the first polled read

print()
hdr = (f"{'seq':>4}  {'verdict':10}  {'advice':26}  {'conf':5}  "
       f"{'cpu_util':8}  {'util_Δ':>10}  {'util_rate':>12}  "
       f"{'temp':7}  {'temp_Δ':>8}")
print(hdr)
print(SEP2)

prev_verdict = None
for i in range(14):
    time.sleep(0.45)
    frame = runtime.sense_reading("compute", shape="features")
    feat  = frame.data or {}

    cpu_util   = fv(feat, "cpu.util_pct")
    util_delta = fv(feat, "cpu.util_pct.delta")
    util_rate  = fv(feat, "cpu.util_pct.rate_per_sec")
    temp       = fv(feat, "thermal.max_temp_c")
    temp_delta = fv(feat, "thermal.max_temp_c.delta")

    changed = "  ← VERDICT CHANGED" if (frame.verdict != prev_verdict
                                          and prev_verdict is not None) else ""
    print(
        f"{frame.seq:4d}  {frame.verdict:10}  {frame.advice:26}  "
        f"{frame.confidence:.3f}  "
        f"{cpu_util:8.4f}  {util_delta:+.6f}  {util_rate:+.10f}  "
        f"{temp:.4f}  {temp_delta:+.6f}"
        f"{changed}"
    )
    prev_verdict = frame.verdict

stop_cpu_burn(burn_procs)
print()
print("  CPU burn stopped. Showing cooldown …")
print()
print(hdr)
print(SEP2)

for i in range(5):
    time.sleep(0.5)
    frame = runtime.sense_reading("compute", shape="features")
    feat  = frame.data or {}

    cpu_util   = fv(feat, "cpu.util_pct")
    util_delta = fv(feat, "cpu.util_pct.delta")
    util_rate  = fv(feat, "cpu.util_pct.rate_per_sec")
    temp       = fv(feat, "thermal.max_temp_c")
    temp_delta = fv(feat, "thermal.max_temp_c.delta")

    changed = "  ← VERDICT CHANGED" if (frame.verdict != prev_verdict
                                          and prev_verdict is not None) else ""
    print(
        f"{frame.seq:4d}  {frame.verdict:10}  {frame.advice:26}  "
        f"{frame.confidence:.3f}  "
        f"{cpu_util:8.4f}  {util_delta:+.6f}  {util_rate:+.10f}  "
        f"{temp:.4f}  {temp_delta:+.6f}"
        f"{changed}"
    )
    prev_verdict = frame.verdict

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — Verdict frame for every offered resource
# ─────────────────────────────────────────────────────────────────────────────

print()
print(SEP)
print("SECTION 3 — verdict frame for every offered resource")
print(SEP)
print()

for res in sorted(runtime.capabilities):
    frame = runtime.sense_reading(res, shape="verdict")
    if frame.data is None:
        print(f"  {res:22s}  [not offered on this device]")
    else:
        print(f"  {res:22s}  verdict={frame.verdict!s:10}  "
              f"advice={frame.advice!s:26}  "
              f"confidence={frame.confidence:.3f}  seq={frame.seq}")

# ─────────────────────────────────────────────────────────────────────────────

print()
print(SEP)
print("SENSE PIPELINE PART 1 OK")
print(SEP)
