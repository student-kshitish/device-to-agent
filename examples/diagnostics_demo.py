"""
diagnostics_demo.py — PHASE 7: the read-only diagnostic surface.

A diagnostic lets an agent SEE a subsystem's failure state BEFORE any fix is
attempted — it is the read-only half of the fix loop (intervention is a later
phase). This demo attaches the four diagnostic families against REAL subsystems
on this Linux machine, then shows an agent:
  1. discovering each diagnostic's manifest (what it sees + cannot_observe),
  2. being DENIED by default (sensitive: system introspection),
  3. binding after owner approval and reading genuine state,
  4. subscribing a boolean-field condition that fires when a device node's
     `present` flips true→false (driven on a controllable FIXTURE node, not the
     real camera).

Linux-only; each diagnostic degrades gracefully where its source is absent.
Loopback, pure stdlib.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

SEP = "=" * 64


def _discover(agent, device, cap):
    ip, port = device.swarm.address
    now = time.time()
    with agent.swarm._lock:
        for c in device.advertise():
            rec = {"node_id": device.node_id, "name": c.name, "tags": list(c.tags),
                   "live_state": dict(c.live_state), "public_key": device.public_key,
                   "address": [ip, port], "device_class": device.device_class, "ts": now}
            man = device._capability_record(c, ip, port).get("manifest")
            if man is not None:
                rec["manifest"] = man
            agent.swarm.records[(device.node_id, c.name)] = rec
    agent.swarm.add_known_peer(device.node_id, ip, port)


def _first_usb():
    base = "/sys/bus/usb/devices"
    if os.path.isdir(base):
        for d in sorted(os.listdir(base)):
            if os.path.isdir(os.path.join(base, d, "power")):
                return d
    return "1-1"


def _first_module():
    try:
        with open("/proc/modules") as f:
            return f.readline().split(" ", 1)[0]
    except OSError:
        return "loop"


print(SEP)
print("DIAGNOSTIC SURFACE DEMO — read-only subsystem self-inspection")
print(SEP)

device = DeviceRuntime(name="diag-host", capability_override=["compute"])
device.start_swarm()
agent = RemoteAgent(name="diag-agent", auto_renew=False)
agent.start()
time.sleep(0.1)

# ── attach four diagnostics against REAL subsystems ──────────────────────────
targets = [
    ("device_node_health",   "/dev/null",           {}),
    ("kernel_module_health", _first_module(),        {}),
    ("service_health",       "systemd-journald",     {}),
    ("usb_power_health",     _first_usb(),            {}),
]
caps = []
for family, target, opts in targets:
    info = device.attach_diagnostic(family, target, **opts)
    caps.append(info["name"])
print()

# ── 1. manifests: what each SEES and CANNOT observe ──────────────────────────
print(SEP); print("1. Manifests — what each diagnostic sees + cannot_observe"); print(SEP)
for cap in caps:
    _discover(agent, device, cap)
    man = agent.describe(cap, device.node_id)
    print(f"\n  {cap}")
    print(f"    sees          : {sorted(man['reading'])}")
    print(f"    cannot_observe:")
    for line in man["cannot_observe"]:
        print(f"        - {line}")
    print(f"    consent_tier  : {man['consent_tier']}")

# ── 2. denied by default (no owner approval) ─────────────────────────────────
print("\n" + SEP); print("2. Sensitive — DENIED to an unapproved remote agent"); print(SEP)
r = agent.bind_remote_to(device.node_id, caps[0])
print(f"  bind {caps[0]} → status={r.get('status')}  code={r.get('code')}")
assert r.get("status") == "denied", r

# ── 3. owner approves → bind + read REAL state ───────────────────────────────
print("\n" + SEP); print("3. Owner approves → agent reads genuine subsystem state"); print(SEP)
device.policy.set_approval_callback(lambda res, aid: True)
for cap in caps:
    b = agent.bind_remote_to(device.node_id, cap)
    if not b.get("verified"):
        print(f"  {cap}: bind failed {b}"); continue
    frame = agent.request_data(b)["frame"]
    src = list(frame["raw"].keys())[0]
    print(f"  {cap}\n    reading = {frame['raw'][src]}")
    agent.release_binding(b)

# ── 4. boolean-field condition fires on a present:true→false transition ──────
print("\n" + SEP); print("4. Condition: notify when a node's present → false"); print(SEP)
fd, node = tempfile.mkstemp(prefix="diag_fixture_"); os.close(fd)
cap = device.attach_diagnostic("device_node_health", node)["name"]
_discover(agent, device, cap)
b = agent.bind_remote_to(device.node_id, cap)
assert b.get("verified"), b

events = []
resp = agent.on_event(b, {"field": "present", "op": "eq", "value": False},
                      lambda e: events.append(e), eval_hz=10)
print(f"  subscribed to present==false on {node}")
time.sleep(0.4)                          # baseline: present=True → no fire
print(f"  baseline events: {len(events)} (expected 0)")
os.remove(node)                          # the node vanishes → present flips false
time.sleep(0.7)
print(f"  after node removed: {len(events)} event(s) fired")
if events:
    snap = events[0]["reading"]["raw"]["device_node_health"]
    print(f"    triggering snapshot: present={snap['present']} observable={snap['observable']}")
assert len(events) == 1, events

print("\n" + SEP)
print("DIAGNOSTIC SURFACE OK — diagnosis is the read-only half of the fix loop.")
print("Intervention (acting on what a diagnostic reveals) is Phase 8.")
print(SEP)

agent.stop()
device.stop_swarm()
print("Done.")
