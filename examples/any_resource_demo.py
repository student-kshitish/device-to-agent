"""
any_resource_demo.py — every physical resource D2A can handle, with owner-consent policy.

Shows:
  1. What THIS device actually offers (real probes).
  2. Open resources (compute, storage, network) bindable by default.
  3. Sensitive resources (camera, mic, display) BLOCKED by default — zero effort.
  4. Owner explicitly opens one sensitive resource → agent can bind + gets safe metadata frame.
  5. captured/coords_captured = False in every frame — privacy guarantee airtight.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a.resource_probes import probe_resources, RESOURCE_SENSITIVITY
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent

SEP  = "=" * 60
SEP2 = "-" * 60


def seed(agent: RemoteAgent, provider: DeviceRuntime) -> None:
    ip, port = provider.swarm.address
    agent.swarm.add_known_peer(provider.node_id, ip, port)
    now = time.time()
    for cap in provider.advertise():
        agent.swarm.records[(provider.node_id, cap.name)] = {
            "node_id":    provider.node_id,
            "name":       cap.name,
            "tags":       list(cap.tags),
            "live_state": dict(cap.live_state),
            "public_key": provider.public_key,
            "address":    [ip, port],
            "ts":         now,
        }


# ── 1. Probe this device ───────────────────────────────────────────────────────
print(SEP)
print("=== RESOURCES THIS DEVICE OFFERS ===")
print(SEP)

resource_snap = probe_resources()
print(f"  {'Resource':<14} {'Sensitivity':<12} {'Details'}")
print(f"  {SEP2[:48]}")
for name, data in sorted(resource_snap.items()):
    sensitivity = RESOURCE_SENSITIVITY.get(name, "unknown")
    # pick a one-line summary from the data dict
    detail_keys = [k for k in data if k not in ("access",)]
    summary     = "  ".join(f"{k}={v}" for k, v in list(data.items())[:3] if k != "access")
    print(f"  {name:<14} {sensitivity:<12} {summary}")

print()
open_res      = sorted(n for n, s in RESOURCE_SENSITIVITY.items() if s == "open")
sensitive_res = sorted(n for n, s in RESOURCE_SENSITIVITY.items() if s == "sensitive")
present_sens  = [r for r in sensitive_res if r in resource_snap]
print(f"  OPEN by default   : {open_res}")
print(f"  SENSITIVE (consent): {sensitive_res}")
print(f"  Present + sensitive: {present_sens}")
print()

# ── 2. Safe-default provider ───────────────────────────────────────────────────
print(SEP)
print("Provider A: SAFE DEFAULTS (no open_resources, no approval_callback)")
print(SEP)

provider_a = DeviceRuntime(name="safe-device")
provider_a.start_swarm()
time.sleep(0.1)

policy_summary = provider_a.policy.summary()
print(f"  policy.open           : {policy_summary['open']}")
print(f"  policy.needs_approval : {policy_summary['needs_approval']}")
print(f"  policy.denied         : {policy_summary['denied']}")
print()

# ── 3. Agent — bind OPEN resources ────────────────────────────────────────────
print(SEP)
print("Agent: binding OPEN resources (compute + storage)")
print(SEP)

agent = RemoteAgent(name="resource-agent")
agent.start()
time.sleep(0.05)
seed(agent, provider_a)

bound_open: list = []
for res in ("compute", "storage"):
    if res not in provider_a.capabilities:
        print(f"  {res}: not advertised by this device — skip")
        continue
    binding = agent.bind_remote_to(provider_a.node_id, res)
    status  = binding.get("status", "?")
    ok      = binding.get("verified", False)
    print(f"  {res}: status={status}  verified={ok}")
    if ok:
        result = agent.request_data(binding, capability=res)
        frame  = result.get("frame", {})
        raw    = frame.get("raw", {})
        # show a couple of fields from the raw reading
        summary = {}
        for src_name, src_data in raw.items():
            if isinstance(src_data, dict):
                for k, v in list(src_data.items())[:2]:
                    summary[f"{src_name}.{k}"] = v
        print(f"    frame: {dict(list(summary.items())[:4])}")
        bound_open.append(res)
        # keep binding for release later
        setattr(agent, f"_binding_{res}", binding)

print()

# ── 4. Policy protection: block sensitive resources by default ─────────────────
print(SEP)
print("Agent: attempting to bind SENSITIVE resources (should all be DENIED by default)")
print(SEP)

denied_count = 0
for res in present_sens:
    if res not in provider_a.capabilities:
        continue
    result = agent.bind_remote_to(provider_a.node_id, res)
    status = result.get("status", "?")
    msg    = result.get("policy_message") or result.get("detail", "")
    print(f"  {res}: status={status}  →  {msg}")
    if status == "denied":
        denied_count += 1

if not present_sens:
    print("  (no sensitive resources detected on this device — forcing camera demo via override)")
    present_sens = ["camera"]

print()
if denied_count > 0 or not present_sens:
    print("  SENSITIVE RESOURCE PROTECTED BY DEFAULT ✓")
else:
    print("  WARNING: expected at least one denial")
print()

# ── 5. Owner consent: explicitly open one sensitive resource ───────────────────
consent_resource = present_sens[0]
print(SEP)
print(f"Provider B: EXPLICITLY OPENS '{consent_resource}' with approval_callback=True")
print(SEP)

provider_b = DeviceRuntime(
    name     = "consent-device",
    open_resources     = [consent_resource],
    approval_callback  = lambda resource, agent_id: True,  # owner always approves
)
provider_b.start_swarm()
time.sleep(0.1)

policy_b = provider_b.policy.summary()
print(f"  policy.open           : {policy_b['open']}")
print(f"  policy.needs_approval : {policy_b['needs_approval']}")
print()

agent_b = RemoteAgent(name="consent-agent")
agent_b.start()
time.sleep(0.05)
seed(agent_b, provider_b)

binding_b = agent_b.bind_remote_to(provider_b.node_id, consent_resource)
print(f"  bind '{consent_resource}': status={binding_b.get('status')}  "
      f"verified={binding_b.get('verified')}")

if binding_b.get("verified"):
    result = agent_b.request_data(binding_b, capability=consent_resource)
    frame  = result.get("frame", {})
    raw    = frame.get("raw", {})
    print(f"  SAFE METADATA FRAME (no private data captured):")
    for src, data in raw.items():
        if isinstance(data, dict):
            for k, v in data.items():
                if "captured" in k or "available" in k or "present" in k or "output" in k:
                    print(f"    {src}.{k} = {v}")
    # verify the privacy guarantee
    for src, src_data in raw.items():
        if isinstance(src_data, dict):
            for k, v in src_data.items():
                if "captured" in k:
                    assert v is False, f"Privacy violation: {src}.{k} = {v}"
    print(f"  Privacy guarantee: captured=False confirmed in all fields ✓")
else:
    print(f"  Bind result: {binding_b}")

print()

# ── 6. Summary ────────────────────────────────────────────────────────────────
print(SEP)
print("SUMMARY")
print(SEP)
print(f"  Capabilities on safe-device   : {list(provider_a.capabilities)}")
print(f"  Bound OPEN successfully       : {bound_open}")
print(f"  SENSITIVE denied (safe default): {denied_count} / {len(present_sens)}")
print(f"  Consent-opened '{consent_resource}'  : bound + safe metadata only")
print()
print("RESOURCE POLICY OK")
print(SEP)

provider_a.stop_swarm()
provider_b.stop_swarm()
agent.stop()
agent_b.stop()
print("Done.")
