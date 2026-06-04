import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.llm_agent import LLMAgent
from d2a import verify_token

runtime = DeviceRuntime(name="bind-one")
agent = LLMAgent()

print(f"Runtime node_id : {runtime.node_id}")
print(f"Agent   agent_id: {agent.agent_id}")
print()

print("Advertised capabilities:")
for cap in runtime.advertise():
    print(f"  {cap.name}: tags={cap.tags}")
print()

# Pick up to two available capabilities, with graceful fallback
available = list(runtime.capabilities)

def pick(preferred: list[str]) -> str:
    for p in preferred:
        if p in available:
            return p
    note = preferred[0]
    print(f"  [note] {note!r} not available on this device — using {available[0]!r} instead")
    return available[0]

cap1 = pick(["gpu", "compute"])
cap2 = pick(["compute", "sensing", "gpu"]) if len(available) > 1 else cap1

token1 = agent.request_bind(runtime, cap1)
print(f"{cap1.upper()} BindToken:")
print(f"  capability : {token1.capability_name}")
print(f"  signature  : {token1.signature[:32]}...")
print(f"Token valid: {verify_token(token1)}")
print()

if cap2 != cap1:
    token2 = agent.request_bind(runtime, cap2)
    print(f"{cap2.upper()} BindToken:")
    print(f"  capability : {token2.capability_name}")
    print(f"  signature  : {token2.signature[:32]}...")
    print(f"Token valid: {verify_token(token2)}")
