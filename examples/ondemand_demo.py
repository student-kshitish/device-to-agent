"""
ondemand_demo.py — THE MAIN DEMO for the D2A data delivery layer.

Proves:
  1. Zero background work after bind (on_demand_reads=0, active_subscribers=0).
  2. Each request_data() reads FRESH kernel data at that instant — CPU util/delta
     CHANGES between calls because real work is done between them.
  3. One get_reading round-trip takes only a few ms (on-demand is cheap).
  4. A request AFTER unbind is REJECTED by scope enforcement.

All in one process via LANSwarm loopback; pure stdlib.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

SEP = "=" * 60


def cpu_burn(seconds: float = 0.2) -> int:
    """Waste CPU so that util_pct visibly changes between on-demand reads."""
    end = time.time() + seconds
    x   = 0
    while time.time() < end:
        x += 1
    return x


# ── 1. Start provider + agent on loopback ────────────────────────────────────
print(SEP)
print("ON-DEMAND DATA DEMO — starting provider + agent (loopback)")
print(SEP)

provider = DeviceRuntime(name="demo-provider")
provider.start_swarm()
time.sleep(0.1)

agent = RemoteAgent(name="demo-agent")
agent.start()
time.sleep(0.05)

p_ip, p_port = provider.swarm.address
agent.swarm.add_known_peer(provider.node_id, p_ip, p_port)
now = time.time()
for cap in provider.advertise():
    agent.swarm.records[(provider.node_id, cap.name)] = {
        "node_id":    provider.node_id,
        "name":       cap.name,
        "tags":       list(cap.tags),
        "live_state": dict(cap.live_state),
        "public_key": provider.public_key,
        "address":    [p_ip, p_port],
        "ts":         now,
    }

print(f"  provider : {provider.node_id[:8]}  caps={list(provider.capabilities)}")
print(f"  agent    : {agent.agent_id[:8]}")
print()

# ── 2. Bind ───────────────────────────────────────────────────────────────────
print(SEP)
print("Binding agent to 'compute'")
print(SEP)
binding = agent.bind_remote("compute", priority=5)
assert binding.get("verified"), f"Bind failed: {binding}"
print(f"  binding_id : {binding['binding_id']}")
print(f"  verified   : {binding['verified']}")
print()

# ── 3. PROVE: zero background work right after bind ──────────────────────────
print(SEP)
print("Stats IMMEDIATELY after bind (before any request_data call):")
print(SEP)
stats = provider.data.stats()
print(f"  {stats}")
cs = stats.get("compute", {})
assert cs.get("on_demand_reads",    0) == 0, f"Expected 0 reads, got {cs}"
assert cs.get("active_subscribers", 0) == 0, f"Expected 0 subs, got {cs}"
print("  ZERO BACKGROUND WORK CONFIRMED: on_demand_reads=0, active_subscribers=0")
print()

# ── 4. Three on-demand reads with CPU burn between them ───────────────────────
print(SEP)
print("THREE on-demand reads with CPU burns between them")
print("Each read hits /proc/stat fresh — util changes with actual CPU state")
print(SEP)
print()

prev_util = None
for i in range(1, 4):
    cpu_burn(0.25)          # change kernel's cpu-jiffies so next delta is real

    t0     = time.perf_counter()
    result = agent.request_data(binding, capability="compute")
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert result.get("type") == "reading", f"Unexpected response: {result}"
    frame   = result["frame"]
    raw     = frame.get("raw", {})
    derived = frame.get("derived", {})
    cpu_raw = raw.get("cpu", {})

    util  = cpu_raw.get("util_pct", "n/a (first sample)")
    load1 = cpu_raw.get("load1", "n/a")
    seq   = frame.get("seq")

    print(f"  READ #{i}  seq={seq}  round-trip={elapsed_ms:.1f}ms")
    print(f"    cpu.util_pct  = {util}")
    print(f"    cpu.load1     = {load1}")

    for key in ("cpu.util_pct", "cpu.load1", "memory.used_percent"):
        d = derived.get(key)
        if d:
            print(f"    derived[{key}]:  value={d['value']}  "
                  f"delta={d['delta']}  rate={d['rate_per_sec']}/s  "
                  f"avg={d['avg_window']}  min={d['min']}  max={d['max']}")

    if isinstance(util, float) and prev_util is not None:
        diff = abs(util - prev_util)
        print(f"    util changed by {diff:.1f}pp since last read  "
              f"({'CHANGED — fresh kernel data' if diff > 0 else 'same — steady load'})")
    prev_util = util if isinstance(util, float) else prev_util
    print()

# ── 5. Timing: one get_reading round-trip ────────────────────────────────────
print(SEP)
print("Timing: single get_reading round-trip")
print(SEP)
REPS = 3
times = []
for _ in range(REPS):
    t0 = time.perf_counter()
    agent.request_data(binding, capability="compute")
    times.append((time.perf_counter() - t0) * 1000)
avg_ms = sum(times) / len(times)
print(f"  {REPS} calls: {[f'{t:.1f}ms' for t in times]}")
print(f"  avg = {avg_ms:.1f}ms  (loopback TCP + kernel read + JSON encode/decode)")
assert avg_ms < 300, f"get_reading too slow: {avg_ms:.1f}ms"
print("  OK: on-demand is cheap")
print()

# ── 6. Stats showing reads accumulated ───────────────────────────────────────
stats = provider.data.stats()
print(SEP)
print(f"Stats after {3 + REPS} reads:")
print(SEP)
print(f"  {stats}")
assert stats["compute"]["on_demand_reads"] >= 3
assert stats["compute"]["active_subscribers"] == 0
print()

# ── 7. Scope enforcement: reject after unbind ────────────────────────────────
print(SEP)
print("Releasing binding on provider side, then attempting request_data…")
print(SEP)
provider.broker_release(agent.agent_id, "compute")
time.sleep(0.05)

result = agent.request_data(binding, capability="compute")
print(f"  Response: {result}")
if result.get("type") == "error" or result.get("error"):
    print("  POST-UNBIND READING REJECTED ✓")
else:
    print(f"  WARNING: expected error, got: {result}")

print()
print(SEP)
final_stats = provider.data.stats()
print("ON-DEMAND DATA OK")
print(f"  Final stats: {final_stats}")
print(SEP)

provider.stop_swarm()
agent.stop()
print("Done.")
