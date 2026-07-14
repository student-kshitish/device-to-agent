"""
intervention_demo.py — PHASE 8: the fix loop end to end.

diagnose (Phase 7) → PLAN → owner APPROVE → EXECUTE → device-run VERIFY → signed
AUDIT. A wrong intervention has no undo, so owner-approval-of-a-PLAN plus a
tamper-evident audit trail stand in for the git safety net that ordinary code
edits enjoy. This demo uses a SAFE, reversible reference fixer: start/stop a
transient user-scope systemd unit (no root), confirmed by the Phase-7
service_health diagnostic.

Shows:
  1. the intervention tier is DENY-BY-DEFAULT (double gate) — an unapproved bind
     is refused, and even an approved bind cannot mutate without per-plan approval;
  2. the owner sees the FULL plan (evidence, expected, verify, reversibility);
  3. on approval the DEVICE executes the fix and runs the declared VERIFY itself
     (never trusted from the agent) — a diagnostic condition that must hold after;
  4. a plan whose verify fails is reported failed_verify, never silent success;
  5. every outcome is written to a signed, hash-chained, append-only audit log.

Linux + user systemd. Loopback, pure stdlib.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent
from d2a.stream_source import ServiceHealthSource

SEP = "=" * 66


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


def _active(unit):
    return ServiceHealthSource(unit, user=True).read().get("active")


# ── a safe, reversible target: a PERSISTENT user unit (start/stop/start cycles) ─
if subprocess.run(["systemctl", "--user", "show-environment"],
                  capture_output=True).returncode != 0:
    print("No user-scope systemd bus available — skipping demo.")
    sys.exit(0)

UNIT     = f"d2a-demo-{os.getpid()}.service"
_UNITDIR = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
os.makedirs(_UNITDIR, exist_ok=True)
with open(os.path.join(_UNITDIR, UNIT), "w") as f:
    f.write("[Unit]\nDescription=D2A intervention demo unit\n"
            "[Service]\nExecStart=/bin/sleep 3600\n")
subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
subprocess.run(["systemctl", "--user", "start", UNIT], capture_output=True)
time.sleep(0.3)


def _cleanup_unit():
    subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
    try:
        os.remove(os.path.join(_UNITDIR, UNIT))
    except OSError:
        pass
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

print(SEP)
print("INTERVENTION DEMO — the read-only diagnosis becomes an owner-approved fix")
print(SEP)
print(f"  target unit: {UNIT}  active={_active(UNIT)}")

device = DeviceRuntime(name="fix-host", capability_override=["compute"])
device.start_swarm()
agent = RemoteAgent(name="fix-agent", auto_renew=False)
agent.start()
time.sleep(0.1)

cap = device.attach_intervention("service_intervene", UNIT)["name"]
_discover(agent, device, cap)

# ── 1. deny-by-default: bind refused with no owner approval ───────────────────
print("\n" + SEP); print("1. Deny-by-default (double gate, layer 1: the bind)"); print(SEP)
r = agent.bind_remote_to(device.node_id, cap)
print(f"  bind (no approval) → {r.get('status')} / {r.get('code')}")

# owner opens the propose-lease
device.policy.set_approval_callback(lambda res, aid: True)
binding = agent.bind_remote_to(device.node_id, cap)
print(f"  bind (owner opts in) → verified={binding.get('verified')}")

# ── 2. propose with NO per-plan approval → denied, state untouched ────────────
print("\n" + SEP); print("2. Double gate, layer 2: per-plan approval"); print(SEP)

def plan(action, want_active):
    inverse = "stop" if action != "stop" else "start"
    return {
        "action": action, "params": {},
        "evidence": {"diagnostic": "service_health", "field": "active",
                     "reading": {"active": _active(UNIT)}},
        "expected": f"unit active={want_active}",
        "verify": {"diagnostic": "service_health",
                   "condition": {"field": "active", "op": "eq", "value": want_active}},
        "reversible": True, "reversible_how": f"systemctl --user {inverse} {UNIT}",
    }

resp = agent.propose_intervention(binding, plan("stop", False))
print(f"  propose stop (no per-plan approval) → {resp['status']} / {resp.get('code')}")
print(f"  unit still active={_active(UNIT)}  (nothing mutated)")

# ── 3. owner approves a SPECIFIC plan → execute + device-run verify ───────────
print("\n" + SEP); print("3. Owner approves the plan → execute + DEVICE verifies"); print(SEP)

def approver(p, agent_id):
    print(f"  [owner] plan: {p['action']}  expected={p['expected']!r}")
    print(f"          evidence={p['evidence']['reading']}  verify={p['verify']['condition']}")
    print(f"          reversible={p['reversible']}  how={p['reversible_how']!r}")
    return True

device.set_intervention_approval_callback(approver)

stop = agent.propose_intervention(binding, plan("stop", False))
print(f"  → status={stop['status']}  executed={stop['executed']}  "
      f"verify_passed={stop['verify']['passed']}  audit_seq={stop['audit_seq']}")
print(f"  diagnostic confirms active={_active(UNIT)}")

start = agent.propose_intervention(binding, plan("start", True))
print(f"  reverse: status={start['status']}  verify_passed={start['verify']['passed']}")
print(f"  diagnostic confirms active={_active(UNIT)}  (real state changed AND reversed)")

# ── 4. a plan whose verify cannot hold → failed_verify (not silent success) ───
print("\n" + SEP); print("4. A fix whose verify fails is NOT reported as success"); print(SEP)
bad = plan("stop", True)   # stop the unit but DECLARE verify active==true
resp = agent.propose_intervention(binding, bad)
print(f"  stop with wrong verify(active==true) → status={resp['status']}  "
      f"executed={resp['executed']}  verify_passed={resp['verify']['passed']}")

# ── 5. the signed, hash-chained, append-only audit trail ─────────────────────
print("\n" + SEP); print("5. Signed append-only audit trail"); print(SEP)
ok, detail = device._audit_log().verify_chain()
print(f"  chain: {detail}  verified={ok}")
for e in device._audit_log().entries():
    print(f"    seq={e['seq']} {e['result_status']:<14} approved={e['approved']} "
          f"verify={e['verify_outcome']:<8} action={e['plan']['action']}")

print("\n" + SEP)
print("FIX LOOP OK — diagnose → plan → approve → execute → verify → audit.")
print("Owner-approval-of-a-plan + signed audit stand in for git's undo.")
print(SEP)

subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
agent.stop()
device.stop_swarm()
print("Done.")
