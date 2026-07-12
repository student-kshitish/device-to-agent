"""
examples/chain_demo.py — CASE 4 Phase 4: MULTI-HOP DERIVATION CHAINING.

Derivations stack. This machine has real `compute`, but no presence sensor and no
occupancy summariser. The planner builds a TWO-HOP chain — compute → presence →
activity_summary — entirely from community recipes, runs it live, and prints the
FULL LINEAGE so you can see (and audit) every hop and every author.

Two scenes:
  1. FULLY LOCAL: one agent instantiates the whole chain in-process (the inner
     `presence` derivation is created and fed locally to `activity_summary`).
  2. ACROSS THE WIRE: agent A derives `presence` and PUBLISHES it; agent B, a
     stranger, derives `activity_summary` from the published presence — to B it is
     an ordinary single-hop derivation onto a provider whose manifest happens to
     say `derived: true`. The chain-max SENSITIVE tier rides along in the manifest.

Trust honesty: a chain means you trust EVERY publisher in the lineage — the
compute host, the presence recipe's author, and the summary recipe's author. The
depth rail (max 2 hops) is a safety rail, not a technical limit: each hop compounds
fidelity loss and widens that trust surface.

Run:  python3 examples/chain_demo.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_HOME = tempfile.mkdtemp(prefix="d2a-chain-demo-")
os.environ["D2A_HOME"] = _HOME

from d2a_derive import Registry, Planner, TrustStore, DerivedCapability
from d2a_derive.planner import MAX_DERIVATION_DEPTH
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

_REF = Path(__file__).resolve().parent.parent / "d2a_derive" / "reference_recipes"


def hr(t):
    print("\n" + "═" * 74 + f"\n  {t}\n" + "═" * 74)


def trusted_registry():
    demo = json.loads((_REF / "DEMO_reference_author.json").read_text())
    trust = TrustStore(path=Path(_HOME) / "trusted_authors.json")
    trust.add(demo["public_key"], demo["name"])
    return Registry(recipes_dir=_REF, trust=trust)


def seed(agent, device, ip, port):
    with agent.swarm._lock:
        for c in device.advertise():
            agent.swarm.records[(device.node_id, c.name)] = \
                device._capability_record(c, ip, port)
    agent.swarm.add_known_peer(device.node_id, ip, port)


def discover_fn(agent):
    def d(name):
        with agent.swarm._lock:
            return [dict(r) for (nid, nm), r in agent.swarm.records.items()
                    if nm == name and isinstance(r.get("manifest"), dict)]
    return d


def print_plan(p):
    print(f"  provides      : {p.provided_name}   (depth {p.depth} hops, rail = {MAX_DERIVATION_DEPTH})")
    print(f"  CONSENT       : effective '{p.effective_tier.upper()}' — chain-max across all hops")
    print(f"  cannot_detect : (UNION of every hop)")
    for c in p.cannot_detect:
        print(f"                  · {c}")
    print(f"  fidelity      : {p.fidelity}")
    print(f"  LINEAGE:")
    for line in p.provenance.lineage_lines():
        print(f"      {line}")


def scene_local():
    hr("1) FULLY LOCAL — compute → presence → activity_summary, one agent")
    dev = DeviceRuntime(name="chain-host", capability_override=["compute", "sensing"],
                        lease_ttl=20)
    dev.start_swarm()
    ag = RemoteAgent(name="chain-agent"); ag.start()
    ip, port = dev.swarm.address
    seed(ag, dev, ip, port)

    pl = Planner(trusted_registry(), discover=discover_fn(ag))
    res = pl.need("activity_summary")
    if res.outcome != "derived":
        print("  refused:", res.code, res.detail); return
    print(f"  planner: no 'activity_summary' and no 'presence' on the wire → CHAIN\n")
    print_plan(res.plan)

    print("\n  running the chain live (one real 'compute' binding feeds the whole stack):")
    dc = DerivedCapability(res.plan, ag).start()
    for _ in range(16):
        time.sleep(0.5)
        r = dc.reading()
        if r:
            print(f"    activity_summary: {r}")
        if r and r.get("samples", 0) >= 5:
            break
    h = dc.health()["per_input"]["presence"]
    print(f"\n  nested health: presence.state={h['state']} "
          f"inner(compute).state={h['inner']['per_input']['compute']['state']}")
    dc.close()
    ag.stop(); dev.stop_swarm()


def scene_wire():
    hr("2) ACROSS THE WIRE — A publishes 'presence', stranger B chains it")
    A_dev = DeviceRuntime(name="A-host", capability_override=["compute", "sensing"],
                          lease_ttl=20, approval_callback=lambda r, a: True)
    A_dev.start_swarm()
    A = RemoteAgent(name="A-agent"); A.start()
    ip, port = A_dev.swarm.address
    seed(A, A_dev, ip, port)
    plA = Planner(trusted_registry(), discover=discover_fn(A))
    inner = DerivedCapability(plA.need("presence").plan, A).start()
    while inner.reading() is None:
        time.sleep(0.2)
    inner.publish(A_dev)
    print(f"  A derived + published 'presence' "
          f"(tier {A_dev.capabilities['presence'].manifest['consent_tier']}, "
          f"derived={A_dev.capabilities['presence'].manifest['derived']})")

    B = RemoteAgent(name="B-agent"); B.start()
    seed(B, A_dev, ip, port)
    plB = Planner(trusted_registry(), discover=discover_fn(B))
    res = plB.need("activity_summary")
    print(f"\n  B.need('activity_summary') → {res.outcome}; to B this is a single hop")
    print(f"  onto a provider whose manifest says derived="
          f"{res.plan.inputs[0]['provider']['manifest'].get('derived')}, "
          f"tier={res.plan.effective_tier} (chain-max preserved on-wire)")
    dc = DerivedCapability(res.plan, B).start()
    for _ in range(16):
        time.sleep(0.5)
        if dc.reading() and dc.reading().get("samples", 0) >= 3:
            break
    print(f"  B live chained reading: {dc.reading()}")
    dc.close(); inner.close()
    A.stop(); B.stop(); A_dev.stop_swarm()


def main():
    print("D2A — Multi-hop Capability Derivation (Phase 4)")
    try:
        scene_local()
        scene_wire()
    finally:
        import shutil
        shutil.rmtree(_HOME, ignore_errors=True)
    hr("DONE")
    print("  A capability two hops removed from any real sensor was synthesised,")
    print("  its full lineage kept auditable, and run live — application layer, no")
    print("  protocol change. You trust every author in that lineage; the rail caps")
    print(f"  the stack at {MAX_DERIVATION_DEPTH} hops so the trust surface stays legible.")


if __name__ == "__main__":
    main()
