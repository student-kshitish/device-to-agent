"""
swarm_dht_demo.py — the LANSwarm multinode demo, mirrored over the Kademlia DHT.

Topology (all on one machine, distinct UDP+TCP ports):
  bootstrap   plain KademliaNode — pure rendezvous/storage, no capabilities
  gpu-box     DeviceRuntime(compute, gpu)      ─┐ bootstrap to `bootstrap`
  edge-cam    DeviceRuntime(compute, sensing)  ─┤ announce capabilities into DHT
  sensor-pi   DeviceRuntime(compute, sensing)  ─┘
  seeker      RemoteAgent                        bootstrap to `bootstrap`

The point this demonstrates: the agent knows ONLY the bootstrap address. It
discovers providers by capability *name* over the DHT and binds to a device it
has never heard of — the device's TCP address arrives inside the DHT record, not
from any prior configuration.

Run:  python3 examples/swarm_dht_demo.py
"""

import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a.kademlia import KademliaNode
from d2a.swarm_dht import DHTSwarm
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

SEP = "=" * 68


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def attach_dht(obj, bootstrap) -> DHTSwarm:
    """Swap a runtime/agent's default LAN transport for a DHTSwarm on its own id."""
    node_id = getattr(obj, "node_id", None) or obj.agent_id
    try:
        obj.swarm._tcp_srv.close()
    except Exception:
        pass
    obj.swarm = DHTSwarm(node_id=node_id, dht_port=free_udp_port(), bootstrap=bootstrap)
    return obj.swarm


# ── 1. Bootstrap rendezvous node ─────────────────────────────────────────────────
print(SEP)
print("Starting DHT bootstrap node...")
print(SEP)
bootstrap = KademliaNode(node_id="dht-bootstrap", udp_port=free_udp_port())
bootstrap.start()
boot_addr = ("127.0.0.1", bootstrap.udp_port)
print(f"  bootstrap listening on UDP {boot_addr[0]}:{boot_addr[1]}\n")


# ── 2. Devices join the DHT and announce capabilities ────────────────────────────
print(SEP)
print("Starting devices (each bootstraps to the rendezvous node)...")
print(SEP)

gpu_box   = DeviceRuntime(name="gpu-box",   capability_override=["compute", "gpu"])
edge_cam  = DeviceRuntime(name="edge-cam",  capability_override=["compute", "sensing"])
sensor_pi = DeviceRuntime(name="sensor-pi", capability_override=["compute", "sensing"])
devices = [gpu_box, edge_cam, sensor_pi]

for d in devices:
    attach_dht(d, boot_addr)
    d.start_swarm()                                     # publishes caps into the DHT
time.sleep(0.8)                                         # let mesh + STOREs settle
print()


# ── 3. Agent joins knowing ONLY the bootstrap address ────────────────────────────
print(SEP)
print("Starting seeker agent (knows only the bootstrap address)...")
print(SEP)
seeker = RemoteAgent(name="seeker")
attach_dht(seeker, boot_addr)
seeker.start()
time.sleep(0.6)
print(f"  seeker id: {seeker.agent_id}")
print(f"  seeker DHT routing table size: {seeker.swarm.routing_size()} peers\n")


# ── 4. Discover by capability name over the DHT ──────────────────────────────────
print(SEP)
print("DISCOVER 'sensing' by name over the DHT")
print(SEP)
sensing = seeker.find_capability("sensing")
sensing = [r for r in sensing if r["node_id"] != seeker.agent_id]
print(f"  found {len(sensing)} sensing provider(s) via DHT FIND_VALUE:")
for r in sensing:
    dev_name = next((d.name for d in devices if d.node_id == r["node_id"]), r["node_id"][:8])
    print(f"    {dev_name:12s}  node={r['node_id'][:8]}  addr={tuple(r['address'])}  "
          f"class={r.get('device_class')}")
print()


# ── 5. Bind with NO prior knowledge of the device's address ──────────────────────
print(SEP)
print("BIND 'sensing' — agent had no prior knowledge of any device address")
print(SEP)

target = sensing[0]
tname = next((d.name for d in devices if d.node_id == target["node_id"]), target["node_id"][:8])
print(f"  target chosen from DHT record: {tname} ({target['node_id'][:8]}) @ {tuple(target['address'])}")

result = seeker.bind_remote("sensing", priority=5)
ok = "✓" if result.get("verified") else "✗"
print(f"  bind_remote('sensing') → status={result.get('status')}  verified={ok}")
print(f"  binding_id={ (result.get('binding_id') or '')[:16] }")
_ttl = result.get("lease_ttl")
_exp = result.get("lease_expires_at", 0)
print(f"  lease: ttl={_ttl}s  expires_at=+{max(0, _exp - time.time()):.0f}s  "
      f"(device clock authoritative; agent auto-renews at ~½ TTL)")
print()


# ── 6. Pull real data over the bound channel ─────────────────────────────────────
print(SEP)
print("PULL one fresh reading over the bound TCP channel")
print(SEP)
reading = seeker.request_data(result, "sensing")
if reading.get("type") == "reading":
    frame = reading.get("frame", {})
    print(f"  reading OK — capability={reading.get('capability')}  "
          f"frame keys={list(frame)}")
else:
    print(f"  reading failed: {reading}")
print()


# ── 7. Tally ─────────────────────────────────────────────────────────────────────
print(SEP)
print("DHT SWARM DEMO OK")
print(SEP)
print(f"  bootstrap nodes : 1")
print(f"  devices         : {len(devices)}")
print(f"  agents          : 1")
print(f"  discovered      : {len(sensing)} sensing provider(s) by name, zero prior addresses")
print(f"  bind verified   : {result.get('verified')}")
print(f"  data pull       : {reading.get('type') == 'reading'}")
print()

for d in devices:
    d.stop_swarm()
seeker.stop()
bootstrap.stop()
print("All nodes stopped.")
