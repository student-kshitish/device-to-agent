"""
examples/composition_wire_demo.py — composition on the wire (Phase B).

A host node publishes a Guardian VirtualSmartObject's SMART surface as a signed,
bindable capability. A remote agent discovers it, reads its manifest, binds it
under a real lease, and drives a manifest-declared action — all over the swarm,
through the normal broker/lease/consent path.

Run:  python3 examples/composition_wire_demo.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("D2A_HOME", tempfile.mkdtemp(prefix="d2a-compose-demo-"))

from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent
from d2a.guardian.relay import DumbRelay
from agents.guardian_agent import GuardianAgent
from d2a.guardian.virtual_object import VirtualSmartObject


def main():
    # A dumb sensor file + a guardian brain = one smart sensor.
    tf = tempfile.NamedTemporaryFile("w", suffix=".val", delete=False)
    tf.write("81000"); tf.close()

    host = DeviceRuntime(name="compose-host", capability_override=["compute"])
    host.start_swarm()
    relay = DumbRelay(node_id=host.node_id, device_path_or_probe=tf.name, kind_override="sensor_file")
    cap = relay.capabilities()[0]
    guardian = GuardianAgent("brain"); guardian.attach(cap)
    vso = VirtualSmartObject(cap, guardian)

    info = host.publish_virtual(vso)
    print(f"\nhost published virtual capability: {info['name']} (access={info['access']})")

    agent = RemoteAgent(name="compose-seeker", auto_renew=False)
    agent.start()
    ip, port = host.swarm.address
    for c in host.advertise():
        agent.swarm.records[(host.node_id, c.name)] = host._capability_record(c, ip, port)
    agent.swarm.add_known_peer(host.node_id, ip, port)

    man = agent.describe("smart_sensor")
    print("\ndiscovered smart_sensor manifest:")
    print(f"  {man['description']}")
    print(f"  actions: {list(man['actions'])}")

    binding = agent.bind_remote_to(host.node_id, "smart_sensor")
    print(f"\nbind verified={binding.get('verified')} status={binding.get('status')}")

    res = agent.call_action(binding, "verdict", {"warn_threshold": 40.0, "danger_threshold": 90.0})
    print("action verdict →", json.dumps(res.get("result"), indent=2))

    agent.release_binding(binding)
    agent.stop(); host.stop_swarm(); os.unlink(tf.name)
    print("\ncomposition-on-wire OK")


if __name__ == "__main__":
    main()
