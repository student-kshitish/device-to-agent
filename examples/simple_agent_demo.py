"""
simple_agent_demo.py — the 5-line agent-author experience.

Shows how an agent author uses the Agent wrapper + ResourceHandle context manager
to find, bind, use, and automatically release a resource in minimal code.

The setup (provider start + loopback seeding) is infrastructure boilerplate
common to any demo. The highlighted core is the 5-line agent pattern.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.simple_agent import Agent

SEP = "=" * 60

# ── Infrastructure: start a provider (this would already exist on the network) ─
print(SEP)
print("SIMPLE AGENT API DEMO")
print(SEP)

provider = DeviceRuntime(name="simple-demo-provider")
provider.start_swarm()
time.sleep(0.1)
print(f"  provider: {provider.node_id[:8]}  caps={list(provider.capabilities)}")
print()

# ── THE 5-LINE AGENT EXPERIENCE ───────────────────────────────────────────────
# (seed_provider replaces UDP discovery for this loopback demo)
print(SEP)
print("5-LINE AGENT PATTERN:")
print("  agent = Agent('my-agent', needs=['compute'])")
print("  agent.start()")
print("  agent.seed_provider(provider)   # replaces UDP discovery on loopback")
print("  with agent.use('compute') as r:")
print("      frame = r.data()")
print(SEP)
print()

agent = Agent("my-agent", needs=["compute"])  # line 1
agent.start()                                  # line 2
agent.seed_provider(provider)                  # line 3 (loopback seeding)

with agent.use("compute") as r:                # line 4 — binds + auto-releases on exit
    result = r.data()                          # line 5 — on-demand fresh frame
    frame  = result.get("frame", {})
    raw    = frame.get("raw", {})
    cpu    = raw.get("cpu", {})
    mem    = raw.get("memory", {})
    print(f"  seq           = {frame.get('seq')}")
    print(f"  cpu.util_pct  = {cpu.get('util_pct', 'pending (first sample)')}")
    print(f"  cpu.load1     = {cpu.get('load1', 'n/a')}")
    print(f"  mem.used_%    = {mem.get('used_percent', 'n/a')}")
    print()
    print(f"  (binding_id = {r.binding().get('binding_id', '?')[:12]}…)")

# After the 'with' block the binding is already released automatically.
print()
print("  ResourceHandle released automatically on __exit__")

# ── Multiple resources in sequence ────────────────────────────────────────────
print()
print(SEP)
print("BINDING MULTIPLE RESOURCES (storage + network if present):")
print(SEP)

for res in ("storage", "network"):
    if res not in provider.capabilities:
        print(f"  {res}: not on this device — skipped")
        continue
    with agent.use(res) as r:
        result = r.data()
        frame  = result.get("frame", {})
        raw    = frame.get("raw", {})
        # one-line summary
        summary_parts = []
        for src, data in raw.items():
            if isinstance(data, dict):
                for k, v in list(data.items())[:2]:
                    summary_parts.append(f"{src}.{k}={v}")
        print(f"  {res}: {', '.join(summary_parts[:3])}")

# ── find() — what's available ─────────────────────────────────────────────────
print()
print(SEP)
print("find() — resources visible in this agent's swarm cache:")
print(SEP)
found = agent.find()
for item in found:
    print(f"  resource={item['resource_name']}  provider={item['provider_node_id'][:8]}")

print()
print(SEP)
print("SIMPLE AGENT API OK")
print(SEP)

agent.stop()
provider.stop_swarm()
print("Done.")
