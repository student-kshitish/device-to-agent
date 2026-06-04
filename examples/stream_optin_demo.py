"""
stream_optin_demo.py — OPT-IN streaming demo.

Proves:
  1. Streaming only starts on explicit subscribe — no background work before it.
  2. Frames arrive at ~5 hz for the subscribed duration.
  3. Loop thread stops cleanly on unsubscribe (active_subscribers → 0, no more frames).
  4. If device has a battery, briefly streams battery_aware too (real kernel state).

All in one process via LANSwarm loopback; pure stdlib.
"""

import sys
import os
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

SEP = "=" * 60


# ── 1. Start provider + agent on loopback ────────────────────────────────────
print(SEP)
print("OPT-IN STREAMING DEMO — starting provider + agent (loopback)")
print(SEP)

provider = DeviceRuntime(name="stream-provider")
provider.start_swarm()
time.sleep(0.1)

agent = RemoteAgent(name="stream-agent")
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
binding = agent.bind_remote("compute", priority=5)
assert binding.get("verified"), f"Bind failed: {binding}"
print(f"  Bound: binding_id={binding['binding_id'][:8]}…")
print()

# ── 3. Confirm: zero background work before subscribe ─────────────────────────
pre_stats = provider.data.stats().get("compute", {})
assert pre_stats.get("active_subscribers", 0) == 0
print(f"  Before subscribe: active_subscribers=0  (no background work yet)")
print()

# ── 4. OPT-IN subscribe at 5 hz for ~3 seconds ───────────────────────────────
print(SEP)
print("OPT-IN: subscribing at hz=5 for ~3 seconds")
print(SEP)

frames_received: list = []
frame_lock = threading.Lock()

def on_frame(frame: dict) -> None:
    with frame_lock:
        frames_received.append(frame)
    seq  = frame.get("seq", "?")
    raw  = frame.get("raw", {})
    util = raw.get("cpu", {}).get("util_pct", "n/a")
    load = raw.get("cpu", {}).get("load1", "n/a")
    mem  = raw.get("memory", {}).get("used_percent", "n/a")
    print(f"  [stream] seq={seq}  cpu.util={util}%  load1={load}  mem={mem}%")

agent.start_stream(binding, on_frame, hz=5.0)
time.sleep(0.3)   # let the loop start and first frame arrive

mid_stats = provider.data.stats().get("compute", {})
print(f"\n  Stats after subscribe: active_subscribers={mid_stats.get('active_subscribers')}")
assert mid_stats.get("active_subscribers", 0) >= 1, "Expected ≥1 subscriber"

# run for ~3 seconds total
time.sleep(2.7)

with frame_lock:
    n_frames = len(frames_received)
print(f"\n  Received {n_frames} frames in ~3 s  (expected ~15 at 5 hz)")
assert n_frames >= 3, f"Too few frames: {n_frames}"
print()

# ── 5. Unsubscribe — confirm loop stops ───────────────────────────────────────
print(SEP)
print("Stopping stream (unsubscribe)…")
print(SEP)

agent.stop_stream(binding)
time.sleep(0.6)   # allow loop thread to exit

post_stats = provider.data.stats().get("compute", {})
print(f"  active_subscribers = {post_stats.get('active_subscribers')}  (expected 0)")
assert post_stats.get("active_subscribers", 0) == 0, "Stream should have stopped"

with frame_lock:
    before = len(frames_received)
time.sleep(0.8)
with frame_lock:
    after = len(frames_received)

new_frames = after - before
print(f"  New frames in 0.8 s after stop: {new_frames}  (expected 0 or at most 1 in-flight)")
assert new_frames <= 1, f"Frames still arriving after stop: {new_frames}"
print("  Stream stopped cleanly — no more frames")
print()

print(SEP)
stream_stats = provider.data.stats()
print("OPT-IN STREAM OK")
print(f"  Final stats: {stream_stats}")
print(SEP)

# ── 6. BONUS: battery_aware streaming if device has a battery ─────────────────
if "battery_aware" in provider.capabilities:
    print()
    print(SEP)
    print("BONUS: battery_aware streaming (real /sys/class/power_supply/BAT*)")
    print(SEP)

    bat_binding = agent.bind_remote("battery_aware", priority=5)
    if bat_binding.get("verified"):
        bat_frames: list = []

        def on_bat(f: dict) -> None:
            bat_frames.append(f)

        agent.start_stream(bat_binding, on_bat, hz=2.0)
        time.sleep(2.0)
        agent.stop_stream(bat_binding)
        provider.broker_release(agent.agent_id, "battery_aware")

        if bat_frames:
            last    = bat_frames[-1]
            bat_raw = last.get("raw", {}).get("battery", {})
            print(f"  capacity : {bat_raw.get('capacity_pct')}%")
            print(f"  status   : {bat_raw.get('status')}")
            print(f"  frames   : {len(bat_frames)} received at ~2 hz")
            print("  (real kernel state from /sys/class/power_supply/BAT*)")
        else:
            print("  (no battery frames received)")
    else:
        print(f"  battery_aware bind failed: {bat_binding}")
    print()
else:
    print("\n  (no battery on this device — battery_aware bonus skipped)")

provider.stop_swarm()
agent.stop()
print("Done.")
