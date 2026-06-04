import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.llm_agent import LLMAgent

runtime    = DeviceRuntime(name="broker-test")
agent_high = LLMAgent()
agent_mid  = LLMAgent()
agent_low  = LLMAgent()

available = list(runtime.capabilities)

# primary cap for quota=1 tests (prefer gpu, fall back to compute)
cap_main = "gpu" if "gpu" in available else available[0]
if cap_main == "gpu":
    print(f"  Using '{cap_main}' (quota=1) for contention tests")
else:
    print(f"  [note] 'gpu' not available — using '{cap_main}' for contention tests")

# secondary cap for quota=2 test (prefer sensing, else anything != cap_main, else reuse cap_main)
cap_quota2 = next(
    (n for n in ("sensing", "gpu", "compute") if n in available and n != cap_main),
    cap_main,
)
if cap_quota2 == cap_main:
    print(f"  [note] only one capability available; quota=2 test reuses '{cap_main}'")
else:
    print(f"  Using '{cap_quota2}' (quota=2) for multi-slot test")

# force quota=2 on the secondary cap for TEST 5
runtime.broker.quotas[cap_quota2] = 2
print()

id_label = {
    agent_high.agent_id: "agent_high",
    agent_mid.agent_id:  "agent_mid",
    agent_low.agent_id:  "agent_low",
}

print(f"agent_high id: {agent_high.agent_id}")
print(f"agent_mid  id: {agent_mid.agent_id}")
print(f"agent_low  id: {agent_low.agent_id}")
print()

# ── TEST 1 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"TEST 1 — QUOTA: agent_mid requests {cap_main!r}")
print("=" * 60)
r1 = runtime.broker_request(agent_mid.agent_id, cap_main, agent_mid.needs, priority=5)
print(f"  status  : {r1['status']}")
print(f"  broker  : {runtime.broker_status()}")
print()

# ── TEST 2 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"TEST 2 — QUEUE: agent_low requests {cap_main!r} (slot full, lower priority)")
print("=" * 60)
r2 = runtime.broker_request(agent_low.agent_id, cap_main, agent_low.needs, priority=9)
print(f"  status         : {r2['status']}")
print(f"  queue_position : {r2.get('queue_position')}")
print()

# ── TEST 3 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"TEST 3 — PREEMPTION: agent_high (priority=1) preempts agent_mid (priority=5)")
print("=" * 60)
r3 = runtime.broker_request(agent_high.agent_id, cap_main, agent_high.needs, priority=1)
preempted_label = id_label.get(r3.get("preempted_agent_id", ""), "?")
print(f"  status : {r3['status']}")
print(f"  PREEMPTED {preempted_label}, GRANTED to agent_high")
print(f"  broker : {runtime.broker_status()}")
print()

# ── TEST 4 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"TEST 4 — RELEASE + AUTO-GRANT: agent_high releases {cap_main!r}")
print("=" * 60)
r4 = runtime.broker_release(agent_high.agent_id, cap_main)
next_label = id_label.get(r4.get("next_agent_id", ""), "none")
print(f"  status : {r4['status']}")
print(f"  AUTO-GRANTED to {next_label}")
print(f"  broker : {runtime.broker_status()}")
print()

# ── TEST 5 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"TEST 5 — QUOTA=2 on {cap_quota2!r}: two agents granted, third queued")
print("=" * 60)
r5a = runtime.broker_request(agent_high.agent_id, cap_quota2, ["sensing"], priority=1)
r5b = runtime.broker_request(agent_mid.agent_id,  cap_quota2, ["sensing"], priority=5)
r5c = runtime.broker_request(agent_low.agent_id,  cap_quota2, ["sensing"], priority=9)
print(f"  agent_high -> {cap_quota2} : {r5a['status']}")
print(f"  agent_mid  -> {cap_quota2} : {r5b['status']}")
print(f"  agent_low  -> {cap_quota2} : {r5c['status']} (position {r5c.get('queue_position')})")
status = runtime.broker_status()
print(f"  active_binds     : {status['active_binds']}")
print(f"  waitqueue_lengths: {status['waitqueue_lengths']}")
print(f"  quotas           : {status['quotas']}")
print()

# ── HISTORY ───────────────────────────────────────────────────────────────────
print("=" * 60)
print("Full broker history")
print("=" * 60)
for entry in runtime.broker.get_history():
    t = entry.pop("time")
    print("  " + "  ".join(f"{k}={v}" for k, v in entry.items()))
    entry["time"] = t
