import sys
import os
import dataclasses
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime
from agents.llm_agent import LLMAgent

# Use the first capability this device actually offers
runtime = DeviceRuntime(name="trust-real")
agent   = LLMAgent()
available = list(runtime.capabilities)
cap_name = "gpu" if "gpu" in available else available[0]
if cap_name != "gpu":
    print(f"  [note] 'gpu' not available — using '{cap_name}' for trust tests")
print()

print("=" * 60)
print("TEST 1: Normal bind flow")
print("=" * 60)
token = agent.request_bind(runtime, cap_name)
print(f"Runtime  node_id   : {runtime.node_id}")
print(f"Agent    agent_id  : {agent.agent_id}")
print(f"Capability         : {token.capability_name}")
print(f"Token signature    : {token.signature[:32]}...")
verified = runtime.verify_agent_token(token)
print(f"Runtime verification: {verified}")
print("TRUST GATE PASSED" if verified else "TRUST GATE FAILED")

print()
print("=" * 60)
print("TEST 2: Tampered token detection")
print("=" * 60)
tampered = dataclasses.replace(token, signature="fakesig123")
tamper_result = runtime.verify_agent_token(tampered)
print(f"Tampered signature  : fakesig123")
print(f"Runtime verification: {tamper_result}")
print("TAMPERED TOKEN DETECTED" if not tamper_result else "TAMPER MISSED (bug!)")

print()
print("=" * 60)
print("TEST 3: Cross-runtime token rejection")
print("=" * 60)
fake_runtime = DeviceRuntime(name="trust-fake")
print(f"Real  runtime node_id : {runtime.node_id}")
print(f"Fake  runtime node_id : {fake_runtime.node_id}")
fake_token = agent.request_bind(fake_runtime, cap_name)
print(f"Token issued by fake runtime, signature: {fake_token.signature[:32]}...")
cross_check = runtime.verify_agent_token(fake_token)
print(f"Real runtime verification: {cross_check}")
print("CROSS-RUNTIME TOKEN REJECTED" if not cross_check else "CROSS-RUNTIME TOKEN ACCEPTED (bug!)")
