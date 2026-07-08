"""
examples/manifest_demo.py — capability manifests (v1.2).

A device publishes capabilities; an agent discovers them and reads each
capability's signed, machine-readable manifest via RemoteAgent.describe() —
learning the reading schema, actions, consent tier and streaming flag WITHOUT
any knowledge of the device's code. This is what a d2a→MCP bridge consumes.

Run:  python3 examples/manifest_demo.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Isolate persisted keys/pins to a tmpdir so the demo never touches ~/.d2a.
os.environ.setdefault("D2A_HOME", tempfile.mkdtemp(prefix="d2a-manifest-demo-"))

from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent


def main():
    device = DeviceRuntime(name="manifest-dev", capability_override=["compute", "sensing", "camera"])
    device.start_swarm()

    agent = RemoteAgent(name="manifest-seeker", auto_renew=False)
    agent.start()

    # Seed discovery deterministically (loopback), then read manifests.
    ip, port = device.swarm.address
    for cap in device.advertise():
        agent.swarm.records[(device.node_id, cap.name)] = device._capability_record(cap, ip, port)
    agent.swarm.add_known_peer(device.node_id, ip, port)

    print("=" * 64)
    print("DISCOVERED CAPABILITIES + MANIFESTS")
    print("=" * 64)
    for cap in device.advertise():
        man = agent.describe(cap.name)
        print(f"\n■ {cap.name}")
        if man is None:
            print("  (no manifest published)")
            continue
        print(f"  description : {man['description']}")
        print(f"  consent     : {man['consent_tier']}   streaming: {man['streaming']}")
        if man.get("reading"):
            print("  reading:")
            for field, spec in man["reading"].items():
                unit = f" [{spec['unit']}]" if spec.get("unit") else ""
                arr = f" of {spec['items']}" if spec.get("type") == "array" else ""
                print(f"    - {field}: {spec['type']}{arr}{unit}")
        if man.get("actions"):
            print("  actions:")
            for name, aspec in man["actions"].items():
                params = ", ".join(aspec.get("params", {}).keys())
                print(f"    - {name}({params})")

    print("\n" + "=" * 64)
    print("Full 'sensing' manifest (note the array-typed fields):")
    print(json.dumps(agent.describe("sensing"), indent=2))

    agent.stop()
    device.stop_swarm()


if __name__ == "__main__":
    main()
