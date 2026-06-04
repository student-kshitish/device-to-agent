"""
Capability Composition — Simple agent API demo.
Shows the ~6-line experience: with agent.achieve("vision") as comp: comp.run()
Proves auto-release on context exit.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtimes.device_runtime import DeviceRuntime
from agents.simple_agent import Agent
from d2a.contracts import IOContract

# ── Build two real runtimes: one camera producer, one GPU consumer ────────────
cam_rt = DeviceRuntime(name="cam-node",  capability_override=["camera"])
gpu_rt = DeviceRuntime(name="gpu-node",  capability_override=["gpu"])

# Attach precise IOContracts so the planner can reason about the pipeline.
# (In production, runtimes advertise these; here we set them directly.)
from d2a.contracts import IOContract, CapabilityContract
cam_rt.capability_contracts["camera"] = CapabilityContract(
    name="camera", role="producer",
    produces=IOContract(media="image", format="raw_rgb", shape=(1280, 720, 3), rate=30.0),
)
gpu_rt.capability_contracts["gpu"] = CapabilityContract(
    name="gpu", role="consumer",
    accepts=IOContract(media="tensor", format="float32", shape=(640, 480, 3)),
)

# ── Create an agent and seed both providers (in-process, no TCP needed) ───────
agent = Agent("vision-agent", needs=["camera", "gpu"])
agent.seed_provider(cam_rt)
agent.seed_provider(gpu_rt)

# ── The ~6-line goal API ───────────────────────────────────────────────────────
print("\nAchieving goal 'vision' …")
with agent.achieve("vision") as vision:
    print(f"  Composition bound: {vision}")
    result = vision.run()
    print(f"  Pipeline ok={result['ok']}  consumer_confirmed={result['consumer_confirmed']}")
    for s in result.get("stages_executed", []):
        role = s["role"]
        if role == "producer":
            print(f"  producer → contract_out={s['contract_out']}")
        elif role == "consumer":
            print(f"  consumer → confirmed={s['consumer_confirmed']}")

print("  Context exited — all bindings auto-released")

# Verify both broker slots are free after context exit
AGENT_ID = agent._remote.agent_id
cam_free = len(cam_rt.broker.active_binds.get("camera", [])) == 0
gpu_free = len(gpu_rt.broker.active_binds.get("gpu",    [])) == 0
print(f"  cam-node/camera free: {cam_free}")
print(f"  gpu-node/gpu    free: {gpu_free}")
assert cam_free and gpu_free, "slots still occupied after context exit!"

print("\nEASY COMPOSE API OK")
