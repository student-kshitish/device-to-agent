import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a import probe_all, available_resources, PROBES
from runtimes.device_runtime import DeviceRuntime

# ── RAW SIGNALS ───────────────────────────────────────────────────────────────
print("=" * 70)
print("=== THIS DEVICE'S REAL SIGNALS ===")
print("=" * 70)
snapshot = probe_all()
print(json.dumps(snapshot, indent=2, default=str))
print()

# ── DYNAMIC RUNTIME ───────────────────────────────────────────────────────────
print("=" * 70)
print("DeviceRuntime — built from probe_all() output")
print("=" * 70)
runtime = DeviceRuntime(name="my-node")
print(f"  device_class : {runtime.device_class}")
print(f"  node_id      : {runtime.node_id}")
print()

print("Advertised capabilities (real live_state):")
for cap in runtime.advertise():
    print(f"  [{cap.name}]  tags={cap.tags}")
    for k, v in cap.live_state.items():
        print(f"    {k}: {v}")
print()

# ── WHAT WAS NOT OFFERED ──────────────────────────────────────────────────────
standard = {"gpu": "gpu", "thermal": "sensing", "sensors": "sensing", "battery": "battery_aware tag"}
print("Standard signals NOT detected on this device:")
absent_any = False
for probe_key, cap_label in standard.items():
    if probe_key not in snapshot:
        print(f"  {probe_key:10s} → None  →  {cap_label} not advertised")
        absent_any = True
if not absent_any:
    print("  (none — all standard probes returned data on this device)")
print()

# ── PORTABILITY NOTE ──────────────────────────────────────────────────────────
print("=" * 70)
print("Portability — same code, zero changes, different devices")
print("=" * 70)
print("""
  Raspberry Pi 4 (4-core ARM, no GPU, no battery, has thermal + hwmon):
    probe_all() → cpu, memory, loadavg, thermal, sensors
    device_class  = sbc_or_pi
    capabilities  = [compute, sensing]
    gpu → None   → gpu not advertised

  Android Termux phone (8-core ARM, battery, thermal, no GPU):
    probe_all() → cpu, memory, battery, thermal
    device_class  = mobile_or_handheld
    capabilities  = [compute, sensing]
    battery_aware tag added to all capabilities

  Headless cloud server (32-core x86_64, no battery, no GPU):
    probe_all() → cpu, memory, loadavg, disk, sensors
    device_class  = generic
    capabilities  = [compute, sensing]
    battery → None, gpu → None → neither advertised

  Drone companion computer (4-core ARM, no battery monitor, has IMU hwmon):
    probe_all() → cpu, memory, sensors (IMU inputs)
    device_class  = sbc_or_pi
    capabilities  = [compute, sensing]""")
print(f"""
  This machine ({snapshot['cpu']['arch']}, {snapshot['cpu']['count']}-core):
    probes found  = {[k for k in snapshot if k not in ('device_class','timestamp')]}
    device_class  = {snapshot['device_class']}
    capabilities  = {list(runtime.capabilities)}""")
print("""
The frozen contract (schema / verbs / identity / broker) never changes.
probe_all() is the only variable. DeviceRuntime advertises only what physically exists.
""")
print(f"available_resources(snapshot): {available_resources(snapshot)}")
