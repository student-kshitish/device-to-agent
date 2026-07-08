import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.llm_agent import LLMAgent
from d2a import verify_bind_token, unbind

runtime = DeviceRuntime(name="rebind-test")
agent   = LLMAgent()

available = list(runtime.capabilities)
cap1 = "gpu" if "gpu" in available else available[0]
cap2 = next((n for n in available if n != cap1), cap1)

if cap1 != "gpu":
    print(f"  [note] 'gpu' not available — using '{cap1}' as primary capability")
if cap2 == cap1:
    print(f"  [note] only one capability; rebind test will renew the same cap")
print()

# ── TEST 1 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"TEST 1 — CREATE BINDING: agent binds {cap1!r} via broker")
print("=" * 60)
r1 = runtime.broker_request(agent.agent_id, cap1, agent.needs, priority=5)
binding_id = r1["binding_id"]
b1 = runtime.broker_get_binding(binding_id)
print(f"  binding_id   : {binding_id}")
print(f"  capability   : {b1['capability_name']}")
print(f"  status       : {b1['status']}")
print(f"  rebind_count : {b1['rebind_count']}")
print()

# ── TEST 2 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print("TEST 2 — RENEW: fresh token, extended TTL")
print("=" * 60)
old_expires = b1["expires_at"]
r2 = runtime.broker_renew(binding_id, ttl_seconds=600)
b2 = runtime.broker_get_binding(binding_id)
print(f"  old expires_at : {old_expires:.4f}")
print(f"  new expires_at : {r2['expires_at']:.4f}")
print(f"  newer          : {r2['expires_at'] > old_expires}")
print(f"  status         : {b2['status']}")
print(f"  rebind_count   : {b2['rebind_count']}  (unchanged)")
print()

# ── TEST 3 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"TEST 3 — REBIND: {cap1!r} -> {cap2!r}")
print("=" * 60)
runtime.broker_rebind(binding_id, cap2)
b3 = runtime.broker_get_binding(binding_id)
rebound_binding = runtime.broker.get_binding(binding_id)
rebound_valid = verify_bind_token(rebound_binding.token, runtime.public_key)
print(f"  capability_name : {b3['capability_name']}")
print(f"  rebind_count    : {b3['rebind_count']}")
print(f"  status          : {b3['status']}")
print(f"  new token valid : {rebound_valid}")
print()

# ── TEST 4 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print("TEST 4 — UNBIND")
print("=" * 60)
unbind(rebound_binding)
print(f"  status : {rebound_binding.status}")
print()

# ── TEST 5 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print("TEST 5 — REBOUND TOKEN STILL CRYPTOGRAPHICALLY VALID")
print("=" * 60)
still_valid = verify_bind_token(rebound_binding.token, runtime.public_key)
print(f"  REBOUND TOKEN VALID: {still_valid}")
print()

# ── LIFECYCLE ─────────────────────────────────────────────────────────────────
print("=" * 60)
print("Binding lifecycle")
print("=" * 60)
bf = runtime.broker.get_binding(binding_id)
journey = f"{cap1}  →  renewed (TTL 600 s)  →  rebound to {cap2}  →  released"
print(f"  binding_id       : {bf.binding_id}")
print(f"  created_at       : {bf.created_at:.4f}")
print(f"  journey          : {journey}")
print(f"  rebind_count     : {bf.rebind_count}")
print(f"  final status     : {bf.status}")
print(f"  token.expires_at : {bf.token.expires_at:.4f}")
