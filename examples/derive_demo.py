"""
examples/derive_demo.py — CASE 4 Phase 2: capability DERIVATION, end to end.

The whole story in one run:

  1. An agent needs a spatial map — the capability `free_space_map` — and NO
     device on the swarm provides it.
  2. The planner finds a community recipe (`trajectory_free_space_map`) that can
     SYNTHESISE it from a motion trajectory, prints the plan, and shows the
     structural consent escalation: open positional inputs → a SENSITIVE derived
     map (mapping a space is sensitive regardless of how open the inputs are).
  3. The derivation runs live: the map GROWS as the (synthetic) device moves, and
     we print the derived reading periodically.
  4. We induce a failure mid-run — the odometry input's lease is killed — and watch
     the self-healer rebind under a fresh lease and the map RESUME (recovery).
  5. We close() cleanly and assert the device shows ZERO residue (no active binds,
     no stream subs).

Then, as the shipped-hardware proof, we derive `ambient_temp` from this machine's
REAL `sensing` capability with `thermal_ambient_proxy` — same engine, a real input.

Trust: the two reference recipes are signed by a clearly-labelled DEMONSTRATION
key whose private seed is public in the repo (that is the point — a signature
proves AUTHORSHIP, not SAFETY; you still choose to trust the author). We add that
demo pubkey to an isolated, throwaway trust store so the recipes load out of the
box without touching your real ~/.d2a.

Run:  python3 examples/derive_demo.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Isolate all persisted D2A state (keys, pins, trust) to a throwaway home BEFORE
# importing anything that reads it — this demo never touches your real ~/.d2a.
_HOME = tempfile.mkdtemp(prefix="d2a-derive-demo-")
os.environ["D2A_HOME"] = _HOME

from d2a_derive import Registry, Planner, TrustStore, DerivedCapability
from d2a_derive.demo_scaffolding import register_demo_odometry, OdometrySource
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

_REF = Path(__file__).resolve().parent.parent / "d2a_derive" / "reference_recipes"


def hr(title):
    print("\n" + "═" * 74)
    print(f"  {title}")
    print("═" * 74)


def seed(agent, device):
    """Deterministic loopback discovery: hand the agent the device's signed
    capability records + address (a UDP announce would do this over a real LAN)."""
    ip, port = device.swarm.address
    with agent.swarm._lock:
        for c in device.advertise():
            agent.swarm.records[(device.node_id, c.name)] = \
                device._capability_record(c, ip, port)
    agent.swarm.add_known_peer(device.node_id, ip, port)


def discover_fn(agent):
    def discover(name):
        with agent.swarm._lock:
            return [dict(r) for (nid, nm), r in agent.swarm.records.items()
                    if nm == name and isinstance(r.get("manifest"), dict)]
    return discover


def trusted_registry():
    """Registry over the SHIPPED reference recipes, with the demo author trusted."""
    demo = json.loads((_REF / "DEMO_reference_author.json").read_text())
    trust = TrustStore(path=Path(_HOME) / "trusted_authors.json")
    trust.add(demo["public_key"], demo["name"])
    reg = Registry(recipes_dir=_REF, trust=trust)
    print(f"  trusted demo author {demo['public_key'][:16]}… "
          f"(seed is PUBLIC — authorship, not safety)")
    print(f"  admitted recipes: {[r.recipe_name for r in reg.loaded]}")
    if reg.rejected:
        print(f"  rejected: {[(r.dir, r.code) for r in reg.rejected]}")
    return reg


def print_plan(res):
    p = res.plan
    print(f"  outcome         : {res.outcome}")
    print(f"  recipe          : {p.recipe.recipe_name} v{p.provenance.version}")
    print(f"  provides        : {p.provided_name}")
    inputs = ", ".join(f"{i['capability']}@{(i['node_id'] or '?')[:8]}"
                       for i in p.provenance.inputs)
    print(f"  binds inputs    : {inputs}")
    input_tiers = [i["provider"]["manifest"].get("consent_tier", "sensitive") for i in p.inputs]
    escalated = any(t != p.effective_tier for t in input_tiers)
    note = (f"ESCALATED from {input_tiers} inputs — a space map is sensitive regardless"
            if escalated else "structural max over inputs + declared output")
    print(f"  CONSENT         : effective '{p.effective_tier.upper()}' ({note})")
    print(f"  fidelity        : {p.fidelity}")
    print(f"  cannot_detect   : {p.cannot_detect}")


def demo_trajectory_map():
    hr("1) An agent needs 'free_space_map' — no device provides it")
    dev = DeviceRuntime(name="demo-mapper", capability_override=["compute", "sensing"],
                        lease_ttl=4)
    # stand up the synthetic positional source (README protocol-gap #2): no shipped
    # capability exposes device position, so we derive from an HONEST demo source.
    register_demo_odometry(dev, source=OdometrySource(step_m=0.6, turn=0.7))
    dev.start_swarm()

    agent = RemoteAgent(name="demo-agent")
    agent.start()
    seed(agent, dev)

    reg = trusted_registry()
    pl = Planner(reg, discover=discover_fn(agent))

    direct = pl.discover("free_space_map")
    print(f"  direct providers of 'free_space_map': {direct}  → none, must derive")

    hr("2) Planner synthesises it from a motion trajectory (+ consent escalation)")
    res = pl.need("free_space_map")
    if res.outcome != "derived":
        print(f"  refused: {res.code} — {res.detail}")
        return
    print_plan(res)

    hr("3) Live derivation — the free-space map grows as the device moves")
    transitions = []
    dc = DerivedCapability(
        res.plan, agent,
        heal_backoff_s=0.2, heal_max_attempts=6,
        on_state_change=lambda o, n, why: transitions.append((o, n, why)),
    ).start()

    def show(tag):
        r = dc.reading()
        h = dc.health()
        pi = h["per_input"]["demo_odometry"]
        print(f"  [{tag:9}] state={h['state']:8} reading={r}  "
              f"stale={pi['staleness_s']}s gaps={pi['gap_count']} rebinds={pi['rebind_count']}")

    for _ in range(6):
        time.sleep(0.6)
        show("running")
    before = dc.reading()["free_cells"]

    hr("4) Induced failure — kill the odometry lease mid-run; healer recovers")
    feed = dc._feeds[0]
    bid = feed.binding["binding_id"]
    print(f"  killing lease {bid[:8]}… (device stays alive → lease_expired branch)")
    b = dev.broker.get_binding(bid)
    b.token = b.token.__class__(**{**b.token.__dict__, "expires_at": time.time() - 1})

    # watch the healer work
    deadline = time.time() + 12
    while time.time() < deadline and feed.rebind_count < 1:
        time.sleep(0.3)
        show("healing")
    for _ in range(6):
        time.sleep(0.5)
        show("resumed")

    after = dc.reading()["free_cells"]
    print(f"\n  recovery: free_cells {before} → {after} "
          f"({'GREW after self-heal ✓' if after > before else 'no growth ✗'}); "
          f"rebind_count={feed.rebind_count}")
    print(f"  state transitions: {transitions}")

    hr("5) Clean close — assert zero device-side residue")
    dc.close()
    time.sleep(0.3)
    active = [ab.agent_id for binds in dev.broker.active_binds.values() for ab in binds]
    print(f"  device active binds after close : {active}")
    print(f"  device stream subs after close  : {dev._binding_subs}")
    assert not active and not dev._binding_subs, "device-side residue after close!"
    print("  ✓ zero residue — every input binding released, every stream torn down")

    agent.stop()
    dev.stop_swarm()


def demo_thermal_real():
    hr("6) Shipped-hardware proof — derive 'ambient_temp' from REAL sensing")
    dev = DeviceRuntime(name="demo-thermal", capability_override=["compute", "sensing"],
                        lease_ttl=6)
    dev.start_swarm()
    agent = RemoteAgent(name="demo-agent-2")
    agent.start()
    seed(agent, dev)

    pl = Planner(trusted_registry(), discover=discover_fn(agent))
    res = pl.need("ambient_temp")
    if res.outcome != "derived":
        print(f"  refused: {res.code} — {res.detail} "
              f"(this machine may expose no thermal 'sensing' capability)")
        agent.stop(); dev.stop_swarm(); return
    print_plan(res)
    print("  → binding the REAL sensing capability (thermal.max_temp_c), live:")

    dc = DerivedCapability(res.plan, agent).start()
    for _ in range(6):
        time.sleep(0.6)
        print(f"    ambient proxy: {dc.reading()}")
    dc.close()
    agent.stop()
    dev.stop_swarm()


def main():
    print("D2A — Capability Derivation (Phase 2): live executor + self-heal + monitor")
    try:
        demo_trajectory_map()
        demo_thermal_real()
    finally:
        import shutil
        shutil.rmtree(_HOME, ignore_errors=True)
    hr("DONE")
    print("  A capability that no device provided was SYNTHESISED, kept alive across")
    print("  a lease loss, and shut down clean — application layer, no wire change.")


if __name__ == "__main__":
    main()
