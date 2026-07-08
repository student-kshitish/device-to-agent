"""
swarm_local_demo.py — single-process sanity test for the swarm layer.

Runs both provider and agent in one script (provider TCP listener in a daemon
thread, agent sends a real TCP bind_request). Proves the full bind flow before
doing the two-terminal cross-machine test.

Discovery is seeded manually (add_known_peer + record injection) because UDP
broadcast loopback is unreliable on the same machine. This is exactly the
add_known_peer() fallback documented in LANSwarm — the same path used when a
router blocks UDP broadcast (AP isolation, Docker, etc.).
"""

import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

SEP = "=" * 60

# ── 1. Provider ───────────────────────────────────────────────────────────────
print(SEP)
print("Starting provider...")
print(SEP)
provider = DeviceRuntime(name="local-provider")
provider.start_swarm()
time.sleep(0.1)   # let TCP listener thread bind

p_ip, p_port = provider.swarm.address
print(f"  node_id  : {provider.node_id}")
print(f"  address  : {p_ip}:{p_port}")
print(f"  caps     : {list(provider.capabilities)}")
print()

# ── 2. Agent ──────────────────────────────────────────────────────────────────
print(SEP)
print("Starting agent...")
print(SEP)
agent = RemoteAgent(name="local-seeker")
agent.start()
time.sleep(0.05)
print(f"  agent_id : {agent.agent_id}")
print()

# ── 3. Seed peer knowledge (simulates UDP announce on a real LAN) ─────────────
print(SEP)
print("Seeding provider into agent (simulates UDP announce broadcast)...")
print(SEP)
agent.swarm.add_known_peer(provider.node_id, p_ip, p_port)

# Inject capability records so discover() returns immediately without waiting
now = time.time()
for cap in provider.advertise():
    record = {
        "node_id":    provider.node_id,
        "name":       cap.name,
        "tags":       list(cap.tags),
        "live_state": {k: v for k, v in cap.live_state.items()},
        "public_key": provider.public_key,
        "address":    [p_ip, p_port],
        "ts":         now,
    }
    agent.swarm.records[(provider.node_id, cap.name)] = record

print(f"  Injected {len(provider.advertise())} capability records into agent cache")
print()

# ── 4. Discover ───────────────────────────────────────────────────────────────
print(SEP)
print("Agent discovering 'compute' from local cache...")
print(SEP)
records = [r for r in agent.swarm.records.values() if r.get("name") == "compute"]
print(f"  Found {len(records)} 'compute' provider(s):")
for r in records:
    nid = r["node_id"]
    print(f"    node={nid[:8]}  address={r['address']}")
    for k, v in list(r.get("live_state", {}).items())[:4]:
        print(f"      {k}: {v}")
print()

# ── 5. Remote bind — real TCP flow ────────────────────────────────────────────
print(SEP)
print("Agent sending bind_request for 'compute' via TCP...")
print(SEP)
result = agent.bind_remote("compute", priority=5)
print()

# ── 6. Result ─────────────────────────────────────────────────────────────────
print(SEP)
if result.get("verified"):
    print("LOCAL SWARM BIND OK")
    print(SEP)
    print(f"  status      : {result['status']}")
    print(f"  binding_id  : {result.get('binding_id')}")
    print(f"  capability  : {result.get('capability_name')}")
    print(f"  provider    : {result.get('node_id')}")
    print(f"  expires_at  : {result.get('expires_at'):.2f}")
    print(f"  verified    : {result['verified']}")
    print()
    print("  Verification note: Ed25519 signature verified + device key TOFU-pinned")
    print("  (node_id derives from the key) + provider-confirmed + not-expired.")
else:
    print(f"BIND FAILED: {result}")
    print(SEP)

print()
provider.stop_swarm()
agent.stop()
print("Done.")
