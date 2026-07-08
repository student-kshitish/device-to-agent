"""
tests/test_manifest.py — capability manifests (v1.2).

Two layers:
  1. TestManifestValidator — the fixed-vocabulary validator + consent SSOT +
     size cap (pure, no transport).
  2. TestManifestWire (+ LAN / DHT concretes) — manifest rides inside the signed
     record: survives a real discovery round-trip with verify_record passing;
     record without a manifest is still valid (additive); tampering the manifest
     breaks verification; UDP and TCP discovery return the same record (the
     unified-builder regression guard). Both transports.

Persisted keys + pins isolated to a tmpdir (never ~/.d2a).
"""

import copy
import json
import socket
import time
import unittest

from d2a import manifest as mf
from d2a.manifest import validate_manifest, ManifestError
from d2a import signing, crypto
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent
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


# ── (1) validator ────────────────────────────────────────────────────────────

class TestManifestValidator(unittest.TestCase):

    def _valid(self):
        return {
            "description": "test cap",
            "reading": {"x": {"type": "number", "unit": "%"},
                        "names": {"type": "array", "items": "string"}},
            "actions": {"go": {"description": "do it",
                               "params": {"n": {"type": "number", "required": True}}}},
            "consent_tier": "open",
            "streaming": True,
        }

    def test_valid_passes(self):
        out = validate_manifest(self._valid(), "open")
        self.assertEqual(out["consent_tier"], "open")
        self.assertTrue(out["streaming"])

    def test_streaming_defaults_false(self):
        m = self._valid(); del m["streaming"]
        self.assertFalse(validate_manifest(m, "open")["streaming"])

    def test_unknown_top_level_key_rejected(self):
        m = self._valid(); m["extra"] = 1
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_missing_description_rejected(self):
        m = self._valid(); del m["description"]
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_bad_type_rejected(self):
        m = self._valid(); m["reading"]["x"]["type"] = "int"
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_array_without_items_rejected(self):
        m = self._valid(); m["reading"]["names"] = {"type": "array"}
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_array_bad_items_type_rejected(self):
        m = self._valid(); m["reading"]["names"] = {"type": "array", "items": "int"}
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_nested_array_rejected(self):
        m = self._valid(); m["reading"]["names"] = {"type": "array", "items": "array"}
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_items_on_non_array_rejected(self):
        m = self._valid(); m["reading"]["x"] = {"type": "number", "items": "number"}
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_format_hex_ok_on_string(self):
        m = self._valid(); m["reading"]["blob"] = {"type": "string", "format": "hex"}
        validate_manifest(m, "open")

    def test_format_hex_on_non_string_rejected(self):
        m = self._valid(); m["reading"]["x"] = {"type": "number", "format": "hex"}
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_bad_format_value_rejected(self):
        m = self._valid(); m["reading"]["blob"] = {"type": "string", "format": "base64"}
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_consent_tier_must_match_ssot(self):
        with self.assertRaises(ManifestError):
            validate_manifest(self._valid(), "sensitive")   # manifest says open

    def test_bad_consent_tier_value_rejected(self):
        m = self._valid(); m["consent_tier"] = "public"
        with self.assertRaises(ManifestError):
            validate_manifest(m, "public")

    def test_action_missing_description_rejected(self):
        m = self._valid(); m["actions"]["go"] = {"params": {}}
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_streaming_must_be_bool(self):
        m = self._valid(); m["streaming"] = "yes"
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_oversized_rejected(self):
        m = self._valid()
        m["reading"] = {f"f{i}": {"type": "string", "description": "x" * 40} for i in range(200)}
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open")

    def test_size_cap_is_enforced_at_boundary(self):
        m = self._valid()
        # comfortably small manifest passes with a tiny cap set high enough
        validate_manifest(m, "open", max_bytes=4096)
        with self.assertRaises(ManifestError):
            validate_manifest(m, "open", max_bytes=10)

    def test_ssot_helpers(self):
        self.assertEqual(mf.consent_tier_for_resource("camera"), "sensitive")
        self.assertEqual(mf.consent_tier_for_resource("compute"), "open")
        self.assertEqual(mf.consent_tier_for_resource("nonexistent"), "sensitive")
        self.assertEqual(mf.consent_tier_for_kind("input_event"), "sensitive")
        self.assertEqual(mf.consent_tier_for_kind("block_fs"), "open")

    def test_builtin_manifests_present_and_valid(self):
        class C:
            def __init__(s, n, ls=None): s.name, s.live_state = n, (ls or {})
        for n in ("compute", "sensing", "camera"):
            self.assertIsNotNone(mf.builtin_manifest(C(n)))
        raw = mf.builtin_manifest(C("raw_block_fs", {"kind": "block_fs"}))
        self.assertEqual(raw["consent_tier"], "open")
        self.assertIn("read_bytes", raw["actions"])
        self.assertIsNone(mf.builtin_manifest(C("gpu")))   # not shipped → None (additive)


# ── (2) manifest on the wire ─────────────────────────────────────────────────

class ManifestWireMixin:
    def setUp(self):
        self.devices, self.agents = [], []
        self._setup_transport()

    def tearDown(self):
        for a in self.agents:
            try: a.stop()
            except Exception: pass
        for d in self.devices:
            try: d.stop_swarm()
            except Exception: pass
        self._teardown_transport()
        time.sleep(0.05)

    def test_manifest_survives_roundtrip_and_verifies(self):
        d = self.make_device(_uniq("dev"), ["compute", "sensing", "camera"])
        a = self.make_agent(_uniq("ag"))
        self._discover(a, d)

        man = a.describe("sensing")
        self.assertIsNotNone(man, "sensing manifest missing after discovery")
        self.assertIn("sample_temps_c", man["reading"])
        self.assertEqual(man["reading"]["sample_temps_c"]["type"], "array")
        self.assertEqual(man["reading"]["sample_temps_c"]["items"], "number")

        # the discovered record still verifies (manifest is inside signed bytes)
        rec = self._record(a, d.node_id, "sensing")
        self.assertIsNotNone(rec)
        self.assertIn("manifest", rec)
        self.assertIsNone(signing.verify_record(rec, a.pins))

    def test_consent_tier_matches_policy(self):
        d = self.make_device(_uniq("dev"), ["compute", "camera"])
        a = self.make_agent(_uniq("ag"))
        self._discover(a, d)
        self.assertEqual(a.describe("compute")["consent_tier"], "open")
        self.assertEqual(a.describe("camera")["consent_tier"], "sensitive")

    def test_tampered_manifest_fails_verification(self):
        d = self.make_device(_uniq("dev"), ["compute"])
        a = self.make_agent(_uniq("ag"))
        self._discover(a, d)
        rec = copy.deepcopy(self._record(a, d.node_id, "compute"))
        self.assertIsNone(signing.verify_record(rec, a.pins))   # baseline ok
        rec["manifest"]["description"] = "malicious rewrite"
        self.assertIsNotNone(signing.verify_record(rec, a.pins))  # now fails

    def test_udp_tcp_record_parity(self):
        # The unified-builder regression guard: publish-path and probe_peer-path
        # records must carry the same manifest + same key set, and both verify.
        d = self.make_device(_uniq("dev"), ["compute", "sensing"])
        a = self.make_agent(_uniq("ag"))
        self._discover(a, d)
        udp_rec = self._record(a, d.node_id, "compute")

        # TCP path: capabilities_request handler → _capability_record
        resp = d._on_message({"type": "capabilities_request", "from_node": "probe"})
        tcp_rec = next(r for r in resp["records"] if r["name"] == "compute")

        self.assertEqual(set(udp_rec), set(tcp_rec), "record key sets differ across transports")
        self.assertIn("manifest", udp_rec)
        self.assertEqual(udp_rec["manifest"], tcp_rec["manifest"])
        self.assertIsNone(signing.verify_record(tcp_rec, a.pins))

    def _record(self, agent, node_id, cap):
        with agent.swarm._lock:
            return agent.swarm.records.get((node_id, cap))


class TestManifestWireLAN(ManifestWireMixin, unittest.TestCase):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass

    def make_device(self, name, caps):
        d = DeviceRuntime(name=name, capability_override=caps, lease_ttl=300)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=name, auto_renew=False)
        a.start()
        self.agents.append(a)
        return a

    def _discover(self, agent, device):
        ip, port = device.swarm.address
        with agent.swarm._lock:
            for cap in device.advertise():
                agent.swarm.records[(device.node_id, cap.name)] = \
                    device._capability_record(cap, ip, port)
        agent.swarm.add_known_peer(device.node_id, ip, port)


class TestManifestWireDHT(ManifestWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="manifest-bootstrap", udp_port=free_udp_port(), ttl=30)
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

    def make_device(self, name, caps):
        d = DeviceRuntime(name=name, capability_override=caps, lease_ttl=300)
        self._attach_dht(d)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=name, auto_renew=False)
        self._attach_dht(a)
        a.start()
        self.agents.append(a)
        time.sleep(0.3)
        return a

    def _discover(self, agent, device):
        agent.find_capability("compute")
        agent.find_capability("sensing")
        agent.find_capability("camera")
        time.sleep(0.2)


# no-manifest record remains valid (additive contract) — transport-independent

class TestNoManifestStillValid(unittest.TestCase):
    def test_record_without_manifest_verifies(self):
        d = DeviceRuntime(name=_uniq("nomani"), capability_override=["gpu"])
        ip, port = d.swarm.address
        # gpu ships no built-in manifest → record has no 'manifest' key, still valid.
        rec = d._capability_record(d.advertise()[0], ip, port)
        self.assertNotIn("manifest", rec)
        self.assertIsNone(signing.verify_record(rec, d.pins))


if __name__ == "__main__":
    unittest.main(verbosity=2)
