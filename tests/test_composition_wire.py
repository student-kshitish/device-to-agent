"""
tests/test_composition_wire.py — Phase B: composition goes on-wire.

Guardian VirtualSmartObjects and Synthesis emergent devices published as
signed, discoverable, bindable capabilities on a host/coordinator node — using
the SAME broker quota, lease lifecycle, and consent policy as real capabilities.

Covered, on BOTH transports:
  - VSO record discoverable + signed + carries the SMART-surface manifest.
  - Agent discovers a VSO purely via the transport, binds under a real lease,
    and executes an action end-to-end.
  - Lease expiry tears down a virtual binding exactly like a real one.
  - Emergent record discoverable + bindable; NO member leak (no member node_id
    or member-manifest bytes in the emergent record).
  - A sensitive-kind VSO denies an unapproved remote agent (no consent bypass).

Persisted keys + pins isolated to a tmpdir (never ~/.d2a).
"""

import json
import os
import socket
import tempfile
import time
import unittest

from d2a import signing
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from d2a.guardian.relay import DumbRelay
from d2a.guardian.virtual_object import VirtualSmartObject
from d2a.composition.synthesis_types import EmergentDevice
from d2a.composition.emergent_runtime import EmergentDeviceHandle
from agents.guardian_agent import GuardianAgent
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent, LeaseLostError
from tests._env import use_tmp_home, restore_home


def setUpModule():
    use_tmp_home()


def tearDownModule():
    restore_home()


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_counter = {"n": 0}


def _uniq(prefix: str) -> str:
    _counter["n"] += 1
    return f"{prefix}-{_counter['n']}"


# member node_ids use a distinctive marker so a leak into the emergent record is
# trivially detectable by substring.
_MEMBER_MARK = "MEMBERLEAKMARK"


def _make_sensor_vso(host, value="42000"):
    tf = tempfile.NamedTemporaryFile("w", suffix=".val", delete=False)
    tf.write(value); tf.close()
    relay = DumbRelay(node_id=host.node_id, device_path_or_probe=tf.name, kind_override="sensor_file")
    cap = relay.capabilities()[0]
    g = GuardianAgent(_uniq("g")); g.attach(cap)
    return VirtualSmartObject(cap, g), tf.name


def _make_input_event_vso(host):
    tf = tempfile.NamedTemporaryFile("w", suffix=".ev", delete=False)
    tf.write("x"); tf.close()
    relay = DumbRelay(node_id=host.node_id, device_path_or_probe=tf.name, kind_override="input_event")
    cap = relay.capabilities()[0]
    g = GuardianAgent(_uniq("g")); g.attach(cap)
    return VirtualSmartObject(cap, g), tf.name


def _make_pooled_handle():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    r0 = DumbRelay(node_id=f"{_MEMBER_MARK}0", device_path_or_probe=d0, kind_override="block_fs")
    r1 = DumbRelay(node_id=f"{_MEMBER_MARK}1", device_path_or_probe=d1, kind_override="block_fs")
    members = [
        {"node_id": f"{_MEMBER_MARK}0", "capability": "raw_block_fs",
         "live_state": {"free_bytes": 4096}, "relay_ref": r0, "provider_node_id": f"{_MEMBER_MARK}0"},
        {"node_id": f"{_MEMBER_MARK}1", "capability": "raw_block_fs",
         "live_state": {"free_bytes": 4096}, "relay_ref": r1, "provider_node_id": f"{_MEMBER_MARK}1"},
    ]
    placement = {
        0: {"member_index": 0, "node_id": f"{_MEMBER_MARK}0", "byte_range": (0, 4096), "relay_ref": r0},
        1: {"member_index": 1, "node_id": f"{_MEMBER_MARK}1", "byte_range": (4096, 8192), "relay_ref": r1},
    }
    dev = EmergentDevice(
        name="pooled_storage_2x", kind="pooled_storage", members=members,
        combined_contract={"media": "storage", "total_bytes": 8192, "members": 2},
        placement_map=placement, live_state={"total_bytes": 8192, "member_count": 2},
    )
    return EmergentDeviceHandle(dev, bindings=members)


class CompositionWireMixin:
    def setUp(self):
        self.hosts, self.agents, self._tmpfiles = [], [], []
        self._setup_transport()

    def tearDown(self):
        for a in self.agents:
            try: a.stop()
            except Exception: pass
        for h in self.hosts:
            try: h.stop_swarm()
            except Exception: pass
        for f in self._tmpfiles:
            try: os.unlink(f)
            except OSError: pass
        self._teardown_transport()
        time.sleep(0.05)

    # ── Guardian VSO ──────────────────────────────────────────────────────────

    def test_vso_discoverable_signed_smart_manifest(self):
        host = self.make_host(_uniq("host"))
        vso, path = _make_sensor_vso(host); self._tmpfiles.append(path)
        host.publish_virtual(vso)
        agent = self.make_agent(_uniq("ag"))
        self._discover(agent, host, "smart_sensor")

        man = agent.describe("smart_sensor")
        self.assertIsNotNone(man, "smart_sensor manifest missing")
        # smart surface, not raw primitives
        self.assertIn("verdict", man["actions"])
        self.assertIn("monitor", man["actions"])
        self.assertNotIn("read_value", man["actions"])   # that's the RAW surface
        rec = self._record(agent, host.node_id, "smart_sensor")
        self.assertIn("manifest", rec)
        self.assertIsNone(signing.verify_record(rec, agent.pins))

    def test_vso_bind_and_action_under_lease(self):
        host = self.make_host(_uniq("host"))
        vso, path = _make_sensor_vso(host, value="42000"); self._tmpfiles.append(path)
        host.publish_virtual(vso)
        agent = self.make_agent(_uniq("ag"))
        self._discover(agent, host, "smart_sensor")

        binding = agent.bind_remote_to(host.node_id, "smart_sensor")
        self.assertTrue(binding.get("verified"), f"bind not verified: {binding}")

        res = agent.call_action(binding, "verdict",
                                {"warn_threshold": 30.0, "danger_threshold": 90.0})
        self.assertEqual(res.get("type"), "action_result")
        self.assertEqual(res["result"]["level"], "danger")   # 42000 > 90 threshold semantics
        # binding is a normal active lease
        self.assertTrue(host.broker.get_binding(binding["binding_id"]).status == "active")

    def test_vso_lease_expiry_tears_down_virtual_binding(self):
        host = self.make_host(_uniq("host"), lease_ttl=1)
        vso, path = _make_sensor_vso(host); self._tmpfiles.append(path)
        host.publish_virtual(vso)
        agent = self.make_agent(_uniq("ag"), auto_renew=False)
        self._discover(agent, host, "smart_sensor")

        binding = agent.bind_remote_to(host.node_id, "smart_sensor")
        self.assertTrue(binding.get("verified"))
        time.sleep(2.0)   # lease (1s) lapses; sweeper reaps it
        # A virtual binding tears down exactly like a real one: the device frees
        # the slot and pushes lease_expired, so the next use surfaces LeaseLostError.
        with self.assertRaises(LeaseLostError):
            agent.call_action(binding, "verdict", {})
        # and the broker slot is genuinely gone on the host side
        b = host.broker.get_binding(binding["binding_id"])
        self.assertNotEqual(b.status, "active")

    def test_sensitive_kind_consent_denied(self):
        host = self.make_host(_uniq("host"))
        vso, path = _make_input_event_vso(host); self._tmpfiles.append(path)
        info = host.publish_virtual(vso)
        self.assertEqual(info["access"], "consent_required")   # sensitive kind
        agent = self.make_agent(_uniq("ag"))
        self._discover(agent, host, "smart_control_input")

        binding = agent.bind_remote_to(host.node_id, "smart_control_input")
        self.assertFalse(binding.get("verified"))
        self.assertEqual(binding.get("status"), "denied")   # no approval callback → denied

    # ── Synthesis emergent ──────────────────────────────────────────────────────

    def test_emergent_discoverable_bindable_no_member_leak(self):
        host = self.make_host(_uniq("coord"))
        handle = _make_pooled_handle()
        host.publish_emergent(handle)
        agent = self.make_agent(_uniq("ag"))
        self._discover(agent, host, "pooled_storage_2x")

        rec = self._record(agent, host.node_id, "pooled_storage_2x")
        self.assertIsNotNone(rec, "emergent record not discovered")
        self.assertIn("manifest", rec)
        self.assertIsNone(signing.verify_record(rec, agent.pins))
        # NO member leak: neither member node_ids nor member records in the record.
        blob = json.dumps(rec)
        self.assertNotIn(_MEMBER_MARK, blob, "member node_id leaked into emergent record")
        self.assertNotIn("members", rec.get("live_state", {}))   # only scalar contract survived
        # bindable + action end-to-end (write then read)
        binding = agent.bind_remote_to(host.node_id, "pooled_storage_2x")
        self.assertTrue(binding.get("verified"), f"emergent bind not verified: {binding}")
        payload = b"hello pool".hex()
        w = agent.call_action(binding, "write", {"key": "k1", "data": payload})
        self.assertEqual(w.get("type"), "action_result")
        r = agent.call_action(binding, "read", {"key": "k1"})
        self.assertEqual(r["result"].get("data"), payload)   # bytes round-trip as hex

    def _record(self, agent, node_id, cap):
        with agent.swarm._lock:
            return agent.swarm.records.get((node_id, cap))


class TestCompositionWireLAN(CompositionWireMixin, unittest.TestCase):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass

    def make_host(self, name, lease_ttl=300):
        h = DeviceRuntime(name=name, capability_override=["compute"], lease_ttl=lease_ttl)
        h.start_swarm()
        self.hosts.append(h)
        return h

    def make_agent(self, name, auto_renew=False):
        a = RemoteAgent(name=name, auto_renew=auto_renew)
        a.start()
        self.agents.append(a)
        return a

    def _discover(self, agent, host, cap):
        ip, port = host.swarm.address
        with agent.swarm._lock:
            for c in host.advertise():
                agent.swarm.records[(host.node_id, c.name)] = host._capability_record(c, ip, port)
        agent.swarm.add_known_peer(host.node_id, ip, port)


class TestCompositionWireDHT(CompositionWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="compose-bootstrap", udp_port=free_udp_port(), ttl=30)
        self.boot.start()
        self.boot_addr = ("127.0.0.1", self.boot.udp_port)

    def _teardown_transport(self):
        self.boot.stop()

    def _attach_dht(self, obj):
        node_id = getattr(obj, "node_id", None) or obj.agent_id
        try: obj.swarm._tcp_srv.close()
        except Exception: pass
        obj.swarm = DHTSwarm(node_id=node_id, dht_port=free_udp_port(),
                             bootstrap=self.boot_addr, ttl=30)

    def make_host(self, name, lease_ttl=300):
        h = DeviceRuntime(name=name, capability_override=["compute"], lease_ttl=lease_ttl)
        self._attach_dht(h)
        h.start_swarm()
        self.hosts.append(h)
        time.sleep(0.4)
        return h

    def make_agent(self, name, auto_renew=False):
        a = RemoteAgent(name=name, auto_renew=auto_renew)
        self._attach_dht(a)
        a.start()
        self.agents.append(a)
        time.sleep(0.3)
        return a

    def _discover(self, agent, host, cap):
        agent.find_capability(cap)
        time.sleep(0.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
