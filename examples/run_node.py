"""
run_node.py — universal provider. Run on ANY machine to join the swarm.

Auto-detects what the device has (CPU, GPU, battery, thermal, etc.) and
advertises it. No configuration needed. Serves bind requests until Ctrl+C.

Usage:
  python3 examples/run_node.py
  python3 examples/run_node.py --name edge-pi
  python3 examples/run_node.py --peers 192.168.1.5:41000,192.168.1.6:41001
"""

import argparse
import socket
import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime

parser = argparse.ArgumentParser(description="D2A node — join swarm as provider")
parser.add_argument("--name",  default=socket.gethostname(), help="node name (default: hostname)")
parser.add_argument("--peers", default="", help="comma-separated ip:port seeds for AP-isolation fallback")
args = parser.parse_args()

runtime = DeviceRuntime(name=args.name)
runtime.start_swarm()
time.sleep(0.1)

# Seed known peers (handles AP-isolation / broadcast-blocked routers)
for entry in filter(None, args.peers.split(",")):
    entry = entry.strip()
    try:
        ip, port_s = entry.rsplit(":", 1)
        records = runtime.swarm.probe_peer(ip, int(port_s))
        print(f"  Probed {entry} → got {len(records)} record(s)")
    except Exception as e:
        print(f"  [warn] Could not probe {entry}: {e}")

p_ip, p_port = runtime.swarm.address
print()
print("=" * 60)
print(f"Node '{runtime.name}' ready")
print("=" * 60)
print(f"  node_id      : {runtime.node_id}")
print(f"  device_class : {runtime.device_class}")
print(f"  address      : {p_ip}:{p_port}")
print(f"  capabilities : {list(runtime.capabilities)}")
for cap in runtime.advertise():
    print(f"    [{cap.name}] tags={cap.tags}")
print()
print(f"Other nodes can join with:  python3 examples/run_node.py --peers {p_ip}:{p_port}")
print(f"Seekers can bind with:      python3 examples/run_seeker.py --peers {p_ip}:{p_port}")
print()
print("Serving... (Ctrl+C to stop)")
print()

tick = 0
try:
    while True:
        time.sleep(5)
        tick += 1
        # Refresh live hardware state and republish
        runtime.refresh_hardware()
        runtime.publish_capabilities()
        # Count other visible nodes
        with runtime.swarm._lock:
            other_nodes = {nid for (nid, _) in runtime.swarm.records if nid != runtime.node_id}
        print(f"  [{runtime.name}] heartbeat #{tick}  peers_visible={len(other_nodes)}  "
              f"active_binds={sum(len(v) for v in runtime.broker.active_binds.values())}")
except KeyboardInterrupt:
    pass
finally:
    runtime.stop_swarm()
    print(f"\nNode '{runtime.name}' stopped.")
