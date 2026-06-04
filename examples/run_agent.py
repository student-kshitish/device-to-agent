"""
run_agent.py — run on MACHINE B (terminal 2).

Discovers capabilities on the LAN, binds 'compute' (present on every device),
and prints the binding result.

Usage:
  python3 examples/run_agent.py                  # pure UDP broadcast discovery
  python3 examples/run_agent.py 192.168.1.5:PORT # add known peer (AP-isolation fallback)

The IP:PORT argument is printed by run_provider.py.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.remote_agent import RemoteAgent

agent = RemoteAgent(name="seeker")
agent.start()

# ── optional peer seed (AP-isolation / broadcast-blocked fallback) ────────────
if len(sys.argv) > 1:
    peer_arg = sys.argv[1]
    try:
        peer_ip, peer_port_str = peer_arg.rsplit(":", 1)
        peer_port = int(peer_port_str)
        print(f"Probing known peer at {peer_ip}:{peer_port} via TCP...")
        records = agent.swarm.probe_peer(peer_ip, peer_port)
        print(f"  Got {len(records)} capability records via TCP probe")
    except Exception as e:
        print(f"  [warn] Could not probe peer: {e}")
    print()

# ── discover ──────────────────────────────────────────────────────────────────
print("Discovering all capabilities on the network...")
all_caps = agent.find_capability()   # None = all, triggers UDP query + 1.5 s wait
print(f"Found {len(all_caps)} capability record(s):")
for r in all_caps:
    print(f"  [{r['name']}]  node={r['node_id'][:8]}  addr={r.get('address')}")
print()

if not all_caps:
    print("No providers found. Make sure run_provider.py is running on the network.")
    print("If UDP broadcast is blocked, pass the provider's IP:PORT as an argument.")
    agent.stop()
    sys.exit(1)

# ── bind compute ──────────────────────────────────────────────────────────────
print("Binding 'compute' remotely...")
result = agent.bind_remote("compute")
print()

if result.get("verified"):
    print("REMOTE BIND VERIFIED")
    print(f"  status      : {result['status']}")
    print(f"  binding_id  : {result.get('binding_id')}")
    print(f"  capability  : {result.get('capability_name')}")
    print(f"  provider    : {result.get('node_id')}")
    print(f"  expires_at  : {result.get('expires_at'):.2f}")
    print(f"  verified    : {result['verified']}")
else:
    print(f"Bind result: {result}")

agent.stop()
