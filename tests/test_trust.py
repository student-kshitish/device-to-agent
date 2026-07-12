"""
tests/test_trust.py — Ed25519 trust gate (Step 2B).

Two layers of coverage:

  1. TestTrustGateUnit — drives DeviceRuntime._on_message directly (no wire, so
     the transport version-stamp can't undo a tampered field). Exercises every
     rejection reason of the device-side trust gate: unsigned trust ops, forged
     signatures, tampered agent_address, version spoofing inside the signed
     payload, replay, TOFU pin violation, and identity-claim (derivation) forgery.

  2. TrustWireMixin (+ LAN / DHT concretes) — full signed bind→renew→release over
     a real transport, and the identity-claim forgery rejected at the protocol
     layer on BOTH transports (the discipline rule).

Persisted keys + pins are isolated to a tmpdir (never ~/.d2a).
"""

import socket
import time
import unittest

from d2a import crypto, signing
from d2a.protocol import PROTOCOL_VERSION
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


def _sign_bind(from_node, priv, pub, cap="compute", ts=None, priority=5, agent_address=None):
    return signing.sign_message({
        "type":            "bind_request",
        "from_node":       from_node,
        "capability_name": cap,
        "needs":           [],
        "priority":        priority,
        "agent_address":   agent_address,
    }, priv, pub, ts=ts)


# ── (1) device-side trust gate, driven directly ──────────────────────────────

class TestTrustGateUnit(unittest.TestCase):

    def setUp(self):
        self.device = DeviceRuntime(name=_uniq("dev"), capability_override=["compute", "sensing"])
        # A legitimate agent identity (real Ed25519 keypair, derived node_id).
        self.priv, self.pub = crypto.generate_keypair()
        self.node = crypto.derive_node_id(self.pub)

    def _bind(self, msg):
        return self.device._on_message(msg)

    # NOTE (v1.4 error-model unification): every trust-denial below now reads the
    # code under the unified `code` key (was `reason`). The VALUES are unchanged —
    # signing.ERR_*/crypto.ERR_* are re-exported verbatim by d2a.errors — so only
    # the carrier key moved. The denial type (bind_response/lease_renewed/released)
    # and status:"denied" are unchanged.

    def test_happy_signed_bind_granted_and_response_signed(self):
        resp = self._bind(_sign_bind(self.node, self.priv, self.pub))
        self.assertIn(resp.get("status"), ("granted", "granted_by_preemption"))
        # response is device-signed and verifies against the device's key
        self.assertTrue(signing.is_signed(resp))
        self.assertEqual(resp["sig_key"], self.device.public_key)
        self.assertTrue(crypto.verify_dict(resp, expected_pubkey_hex=self.device.public_key))

    def test_unsigned_bind_rejected(self):
        resp = self._bind({"type": "bind_request", "from_node": self.node,
                           "capability_name": "compute", "needs": [], "priority": 5})
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], signing.ERR_UNSIGNED)

    def test_unsigned_renew_rejected(self):
        resp = self._bind({"type": "renew_binding", "from_node": self.node,
                           "binding_id": "whatever", "capability_name": "compute"})
        self.assertEqual(resp["type"], "lease_renewed")
        self.assertEqual(resp["code"], signing.ERR_UNSIGNED)

    def test_unsigned_release_rejected(self):
        resp = self._bind({"type": "release_binding", "from_node": self.node,
                           "capability_name": "compute"})
        self.assertEqual(resp["type"], "released")
        self.assertEqual(resp["code"], signing.ERR_UNSIGNED)

    def test_forged_signature_rejected(self):
        msg = _sign_bind(self.node, self.priv, self.pub)
        sig = bytearray(bytes.fromhex(msg["sig"]))
        sig[0] ^= 0x01
        msg["sig"] = bytes(sig).hex()
        resp = self._bind(msg)
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], signing.ERR_BAD_SIG)

    def test_tampered_agent_address_detected(self):
        msg = _sign_bind(self.node, self.priv, self.pub, agent_address=["10.0.0.1", 5000])
        msg["agent_address"] = ["6.6.6.6", 6666]     # attacker rewrites the return path
        resp = self._bind(msg)
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], signing.ERR_BAD_SIG)

    def test_version_spoof_inside_payload_detected(self):
        msg = _sign_bind(self.node, self.priv, self.pub)
        # tamper `v` to a value that differs from what was SIGNED (which is the
        # current PROTOCOL_VERSION). Same major so the transport gate would pass,
        # but the signature covers `v`, so verification must fail.
        msg["v"] = "1.99"
        resp = self._bind(msg)
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], signing.ERR_BAD_SIG)

    def test_replayed_old_ts_rejected(self):
        old = time.time() - (signing.REPLAY_WINDOW_SECONDS + 60)
        resp = self._bind(_sign_bind(self.node, self.priv, self.pub, ts=old))
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], signing.ERR_STALE)

    def test_tofu_pin_violation_rejected(self):
        # node_id was previously pinned to a DIFFERENT key on this device.
        _, other_pub = crypto.generate_keypair()
        self.device.pins._pins[self.node] = other_pub
        resp = self._bind(_sign_bind(self.node, self.priv, self.pub))
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], crypto.ERR_PIN)

    def test_identity_claim_forgery_rejected(self):
        # Attacker signs with its OWN key but claims a victim's node_id.
        victim = "deadbeefdeadbeef"
        resp = self._bind(_sign_bind(victim, self.priv, self.pub))
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], crypto.ERR_DERIVATION)

    def test_data_path_unsigned_still_works(self):
        # get_reading is NOT a trust op — a 1.0-style unsigned message with a
        # valid binding still returns data (ruling #2 / additive-only data path).
        granted = self._bind(_sign_bind(self.node, self.priv, self.pub))
        binding_id = granted["binding_id"]
        reading = self._bind({"type": "get_reading", "v": "1.0",
                              "from_node": self.node, "binding_id": binding_id,
                              "capability": "compute"})
        self.assertEqual(reading["type"], "reading")
        self.assertEqual(reading["binding_id"], binding_id)


# ── (2) full lifecycle + forgery over a real transport, both transports ──────

class TrustWireMixin:
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

    def test_full_signed_lifecycle(self):
        d = self.make_device(_uniq("dev"), ["compute", "sensing"])
        a = self.make_agent(_uniq("ag"))
        self._discover(a, d, "sensing")

        binding = a.bind_remote_to(d.node_id, "sensing")
        self.assertTrue(binding.get("verified"), f"bind not verified: {binding}")
        self.assertNotIn("trust_error", binding)
        self.assertIn(binding.get("status"), ("granted", "granted_by_preemption"))

        # renew (signed) extends the lease
        renewed = self._signed_renew(a, binding)
        self.assertEqual(renewed.get("status"), "renewed")

        # a fresh data pull works after renew
        reading = a.request_data(binding, "sensing")
        self.assertEqual(reading.get("type"), "reading")

        # release (signed)
        rel = a.release_binding(binding)
        self.assertEqual(rel.get("status"), "released")

    def test_identity_claim_forgery_rejected_over_wire(self):
        d = self.make_device(_uniq("dev"), ["compute", "sensing"])
        a = self.make_agent(_uniq("ag"))          # honest agent, used only as a sender
        self._discover(a, d, "compute")

        # Attacker crafts a bind_request signed by its own key but claiming a
        # victim node_id, and injects it through the agent's transport.
        atk_priv, atk_pub = crypto.generate_keypair()
        forged = _sign_bind("victimvictim0000", atk_priv, atk_pub, cap="compute")
        resp = a.swarm.send_and_recv(d.node_id, forged, timeout=5.0)
        self.assertIsNotNone(resp, "no response from device")
        self.assertEqual(resp.get("status"), "denied")
        self.assertEqual(resp.get("code"), crypto.ERR_DERIVATION)

    def _signed_renew(self, agent, binding):
        msg = signing.sign_message({
            "type": "renew_binding", "from_node": agent.agent_id,
            "binding_id": binding["binding_id"], "capability_name": binding["capability_name"],
        }, agent.private_key, agent.public_key)
        return agent.swarm.send_and_recv(binding["provider_node_id"], msg, timeout=5.0)


class TestTrustWireLAN(TrustWireMixin, unittest.TestCase):
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

    def _discover(self, agent, device, cap):
        ip, port = device.swarm.address
        now = time.time()
        with agent.swarm._lock:
            for c in device.advertise():
                agent.swarm.records[(device.node_id, c.name)] = {
                    "node_id": device.node_id, "name": c.name,
                    "tags": list(c.tags), "live_state": dict(c.live_state),
                    "public_key": device.public_key, "address": [ip, port],
                    "device_class": device.device_class, "ts": now,
                }
        agent.swarm.add_known_peer(device.node_id, ip, port)


class TestTrustWireDHT(TrustWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="trust-bootstrap", udp_port=free_udp_port(), ttl=30)
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

    def _discover(self, agent, device, cap):
        agent.find_capability(cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
