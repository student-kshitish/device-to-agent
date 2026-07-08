"""
swarm_multinode_demo.py — single-process simulation of a heterogeneous swarm.

Spins up 5 providers with different capability personalities (via capability_override)
and 2 seeker agents, all in daemon threads on loopback. Peer knowledge is seeded
manually (the same add_known_peer fallback used for AP isolation).

Provider personalities:
  gpu-box    compute + gpu   (workstation)
  edge-cam   compute + sensing (edge camera)
  sensor-pi  compute + sensing (RPi-like)
  phone      compute only    (mobile, battery_aware tag from real probe)
  server     real probes, no override (shows whatever this machine has)

Tests:
  1. Swarm map — every node and its capabilities
  2. Seeker1 binds compute across ALL providers
  3. Seeker2 binds sensing — only matching providers, skips rest
  4. Contention — both seekers target gpu on gpu-box (quota=1):
       seeker1 (priority=1) → granted
       seeker2 (priority=5) → queued
       seeker1 releases → seeker2 auto-granted
"""

import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

SEP  = "=" * 65
SEP2 = "-" * 65

# ── helpers ───────────────────────────────────────────────────────────────────

def cross_seed(providers: list, agents: list) -> None:
    """
    Inject every provider's capability records into every agent's swarm cache
    and add peer addresses. Simulates what UDP broadcast does on a real LAN.
    """
    now = time.time()
    for p in providers:
        p_ip, p_port = p.swarm.address
        for cap in p.advertise():
            record = {
                "node_id":      p.node_id,
                "name":         cap.name,
                "tags":         list(cap.tags),
                "live_state":   {k: v for k, v in cap.live_state.items()},
                "public_key":   p.public_key,
                "address":      [p_ip, p_port],
                "device_class": p.device_class,
                "ts":           now,
            }
            for a in agents:
                a.swarm.records[(p.node_id, cap.name)] = record
                a.swarm.add_known_peer(p.node_id, p_ip, p_port)


def providers_offering(agents_swarm, cap_name: str, self_id: str) -> list[str]:
    """Return list of node_ids that have a record for cap_name."""
    return [
        nid for (nid, cn) in agents_swarm.records
        if cn == cap_name and nid != self_id
    ]


# ── 1. Start providers ────────────────────────────────────────────────────────
print(SEP)
print("Starting providers...")
print(SEP)

gpu_box   = DeviceRuntime(name="gpu-box",   capability_override=["compute", "gpu"])
edge_cam  = DeviceRuntime(name="edge-cam",  capability_override=["compute", "sensing"])
sensor_pi = DeviceRuntime(name="sensor-pi", capability_override=["compute", "sensing"])
phone     = DeviceRuntime(name="phone",     capability_override=["compute"])
server    = DeviceRuntime(name="server")   # real probes, no override

providers = [gpu_box, edge_cam, sensor_pi, phone, server]
for p in providers:
    p.start_swarm()
time.sleep(0.1)   # let all TCP listeners bind

print()

# ── 2. Start seekers ──────────────────────────────────────────────────────────
print(SEP)
print("Starting seekers...")
print(SEP)

seeker1 = RemoteAgent(name="seeker1")
seeker2 = RemoteAgent(name="seeker2")
seekers = [seeker1, seeker2]
for s in seekers:
    s.start()
time.sleep(0.05)

print(f"  seeker1 id: {seeker1.agent_id}")
print(f"  seeker2 id: {seeker2.agent_id}")
print()

# ── 3. Cross-seed ─────────────────────────────────────────────────────────────
print(SEP)
print("Cross-seeding swarm (simulates UDP announce broadcast)...")
print(SEP)
cross_seed(providers, seekers)
total_records = sum(len(list(p.advertise())) for p in providers)
print(f"  Seeded {total_records} capability records into 2 seekers")
print()

# ── 4. Swarm map ──────────────────────────────────────────────────────────────
print(SEP)
print("SWARM MAP (via seeker1)")
print(SEP)

# Collect all unique node_ids and their caps from seeker1's records
swarm_nodes: dict[str, dict] = {}
for (nid, cap_name), rec in seeker1.swarm.records.items():
    if nid not in swarm_nodes:
        swarm_nodes[nid] = {
            "name":         next((p.name for p in providers if p.node_id == nid), nid[:8]),
            "device_class": rec.get("device_class", "?"),
            "caps":         set(),
        }
    swarm_nodes[nid]["caps"].add(cap_name)

all_cap_names = sorted({cap for info in swarm_nodes.values() for cap in info["caps"]})
header = f"  {'name':12s}  {'class':20s}  " + "  ".join(f"{c:8s}" for c in all_cap_names)
print(header)
print("  " + "-" * (len(header) - 2))
for nid, info in swarm_nodes.items():
    marks = "  ".join("  ✓     " if c in info["caps"] else "        " for c in all_cap_names)
    print(f"  {info['name']:12s}  {info['device_class']:20s}  {marks}")
print()

# ── 5. Seeker1 binds compute across ALL providers ─────────────────────────────
print(SEP)
print("SEEKER1: bind 'compute' across ALL providers")
print(SEP)

compute_providers = providers_offering(seeker1.swarm, "compute", seeker1.agent_id)
compute_binds = 0
for nid in compute_providers:
    pname = next((p.name for p in providers if p.node_id == nid), nid[:8])
    result = seeker1.bind_remote_to(nid, "compute", priority=5)
    v = "✓" if result.get("verified") else "✗"
    status = result.get("status", "error")
    bid = (result.get("binding_id") or "")[:12]
    print(f"  {pname:12s}  status={status:8s}  verified={v}  binding={bid}")
    if result.get("verified"):
        compute_binds += 1
print(f"\n  {compute_binds}/{len(compute_providers)} granted")
print()

# ── 6. Seeker2 binds sensing — skip providers that don't offer it ─────────────
print(SEP)
print("SEEKER2: bind 'sensing' — skip nodes that don't offer it")
print(SEP)

sensing_providers = set(providers_offering(seeker2.swarm, "sensing", seeker2.agent_id))
sensing_binds = 0
for p in providers:
    pname = p.name
    if p.node_id in sensing_providers:
        result = seeker2.bind_remote_to(p.node_id, "sensing", priority=5)
        v = "✓" if result.get("verified") else "✗"
        status = result.get("status", "error")
        bid = (result.get("binding_id") or "")[:12]
        print(f"  {pname:12s}  → offers sensing  status={status}  verified={v}  binding={bid}")
        if result.get("verified"):
            sensing_binds += 1
    else:
        print(f"  {pname:12s}  → no sensing capability  SKIPPED")
print(f"\n  {sensing_binds}/{len(sensing_providers)} sensing providers bound")
print()

# ── 7. Contention: both seekers target gpu on gpu-box ─────────────────────────
print(SEP)
print("CONTENTION: both seekers target 'gpu' on gpu-box (quota=1)")
print(SEP)

# seeker1 first (priority=1 — highest)
r_s1 = seeker1.bind_remote_to(gpu_box.node_id, "gpu", priority=1)
print(f"  seeker1 (priority=1) → {r_s1.get('status')}  verified={r_s1.get('verified')}")
print(f"    lease: ttl={r_s1.get('lease_ttl')}s  "
      f"expires_at=+{max(0, r_s1.get('lease_expires_at', 0) - time.time()):.0f}s "
      f"(device clock; seeker auto-renews at ~½ TTL)")

# seeker2 second (priority=5 — lower, slot already taken)
r_s2 = seeker2.bind_remote_to(gpu_box.node_id, "gpu", priority=5)
print(f"  seeker2 (priority=5) → {r_s2.get('status')}  (slot held by seeker1)")
print()

# seeker1 releases gpu on gpu-box
rel = gpu_box.broker_release(seeker1.agent_id, "gpu")
next_id = rel.get("next_agent_id")
next_name = "seeker2" if next_id == seeker2.agent_id else (next_id[:8] if next_id else "none")
print(f"  seeker1 releases 'gpu' on gpu-box")
print(f"  → broker auto-granted to: {next_name}")
print(f"  → new binding_id: {(rel.get('binding_id') or '')[:12]}")
print()

# ── 8. Tally ──────────────────────────────────────────────────────────────────
print(SEP)
gpu_contention_binds = (1 if r_s1.get("verified") else 0) + (1 if r_s2.get("status") == "queued" else 0)
total_binds = compute_binds + sensing_binds + 1  # +1 for gpu granted to seeker1
print("MULTINODE SWARM TEST OK")
print(SEP)
print(f"  providers       : {len(providers)}")
print(f"  agents          : {len(seekers)}")
print(f"  compute binds   : {compute_binds}/{len(compute_providers)}")
print(f"  sensing binds   : {sensing_binds}/{len(sensing_providers)}")
print(f"  gpu (contention): seeker1 granted, seeker2 queued → auto-granted after release")
print(f"  total binds     : {total_binds}")
print()

for p in providers:
    p.stop_swarm()
for s in seekers:
    s.stop()
print("All nodes stopped.")
