"""
run_seeker.py — universal agent. Discover the swarm and bind across providers.

Usage:
  python3 examples/run_seeker.py
  python3 examples/run_seeker.py --want gpu
  python3 examples/run_seeker.py --want compute --peers 192.168.1.5:41000 --count 3
"""

import argparse
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.remote_agent import RemoteAgent

parser = argparse.ArgumentParser(description="D2A seeker — discover and bind capabilities")
parser.add_argument("--want",  default="compute", help="capability to bind (default: compute)")
parser.add_argument("--peers", default="", help="comma-separated ip:port seeds (AP-isolation fallback)")
parser.add_argument("--count", type=int, default=0, help="max providers to bind (0 = all found)")
args = parser.parse_args()

agent = RemoteAgent(name="seeker")
agent.start()

# Seed known peers
for entry in filter(None, args.peers.split(",")):
    entry = entry.strip()
    try:
        ip, port_s = entry.rsplit(":", 1)
        records = agent.swarm.probe_peer(ip, int(port_s))
        print(f"Probed {entry} → got {len(records)} record(s)")
    except Exception as e:
        print(f"[warn] Could not probe {entry}: {e}")

# ── Discover all capabilities ─────────────────────────────────────────────────
print()
print("Discovering all capabilities on the network...")
all_records = agent.find_capability()   # triggers UDP query + 1.5 s wait
print(f"Found {len(all_records)} record(s) total\n")

if not all_records:
    print("No providers found.")
    print("  • Make sure run_node.py is running somewhere on the network.")
    print("  • If UDP broadcast is blocked, pass --peers ip:port")
    agent.stop()
    sys.exit(1)

# Group by node_id → build swarm map
nodes: dict[str, dict] = {}
for r in all_records:
    nid = r["node_id"]
    if nid not in nodes:
        nodes[nid] = {"device_class": r.get("device_class", "?"), "caps": set(), "address": r.get("address")}
    nodes[nid]["caps"].add(r["name"])

print("=== SWARM MAP ===")
print(f"  {'node_id':10s}  {'class':20s}  {'address':22s}  capabilities")
print("  " + "-" * 72)
for nid, info in nodes.items():
    addr_str = f"{info['address'][0]}:{info['address'][1]}" if info.get("address") else "?"
    caps_str = ", ".join(sorted(info["caps"]))
    print(f"  {nid[:10]:10s}  {info['device_class']:20s}  {addr_str:22s}  {caps_str}")
print()

# ── Bind --want across providers ──────────────────────────────────────────────
# Find all distinct provider node_ids that offer the requested capability
providers_with_cap = {
    nid: info for nid, info in nodes.items()
    if args.want in info["caps"]
}

n_found = len(providers_with_cap)
n_target = args.count if args.count > 0 else n_found

print(f"=== BINDING '{args.want}' across {n_target} of {n_found} provider(s) ===")
succeeded = 0
for nid, info in list(providers_with_cap.items())[:n_target]:
    result = agent.bind_remote_to(nid, args.want)
    status = result.get("status", "error")
    v = "✓" if result.get("verified") else "✗"
    dc = result.get("device_class", info["device_class"])
    bid = result.get("binding_id", "-")[:12] if result.get("binding_id") else "-"
    print(f"  node={nid[:8]}  class={dc:15s}  status={status:8s}  verified={v}  binding={bid}")
    if result.get("verified"):
        succeeded += 1

print()
print(f"MULTI-NODE BIND COMPLETE: bound {succeeded} of {n_target} providers")

agent.stop()
