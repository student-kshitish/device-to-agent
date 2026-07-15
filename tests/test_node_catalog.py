"""
tests/test_node_catalog.py — node capability catalog (Phase 9, v1.8).

describe_node is the MCP list_tools / A2A agent-card equivalent at the node
level: ask a reachable node WHAT IT CAN DO, with zero prior capability-name
knowledge. Coverage:

  1. TestNodeCatalogUnit — drives DeviceRuntime._on_message / _node_descriptor
     directly (no wire, so nothing can undo a filtering decision). The
     information-disclosure surface: full open catalog + node header, sensitive /
     intervention OMITTED for the unauthorized (count + names), owner allow()
     reveals in BOTH surfaces, the ONE-PREDICATE flip (visibility tracks
     policy.check exactly), the pure-READ guarantee (describe never prompts the
     owner), live-registry reflection, names-record size discipline + truncation,
     and host-key signature + tamper rejection.

  2. NodeCatalogWireMixin (+ LAN / DHT concretes) — describe_node over a real
     transport (verified + TOFU-pinned), and node_capabilities() enumeration with
     zero prior name knowledge. The DHT concrete additionally asserts the signed
     node:<id> descriptor omits sensitive names.

Persisted keys + pins are isolated to a tmpdir (never ~/.d2a).
"""

import socket
import time
import unittest

from d2a import crypto, signing
from d2a.protocol import PROTOCOL_VERSION
from d2a.swarm import LANSwarm
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from runtimes import device_runtime
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


_UNIQ = [0]


def _uniq(prefix: str) -> str:
    _UNIQ[0] += 1
    return f"{prefix}-{_UNIQ[0]}-{int(time.time()*1000) % 100000}"


# ── 1. unit: the filtering surface, driven directly ─────────────────────────────

class TestNodeCatalogUnit(unittest.TestCase):
    """No wire: call the handler / descriptor builder in-process."""

    def setUp(self):
        # compute + sensing are open; camera is sensitive (needs_approval).
        self.dev = DeviceRuntime(
            name=_uniq("catdev"),
            capability_override=["compute", "sensing", "camera"],
            transport=LANSwarm(node_id="unused"),
        )

    def _describe(self, requester="agentzero"):
        resp = self.dev._on_message({"type": "describe_node", "from_node": requester})
        self.assertEqual(resp["type"], "describe_node_response")
        return resp

    def _names(self, resp):
        return sorted(c["name"] for c in resp["catalog"])

    def test_full_open_catalog_with_manifests_and_header(self):
        resp = self._describe()
        # node self-descriptor header (ruling 2)
        hdr = resp["node"]
        self.assertEqual(hdr["node_id"], self.dev.node_id)
        self.assertEqual(hdr["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(hdr["device_class"], self.dev.device_class)
        self.assertEqual(hdr["host_pubkey"], self.dev.public_key)
        self.assertIn("catalog_ts", hdr)
        self.assertFalse(hdr["catalog_truncated"])
        self.assertNotIn("owner_pubkey", hdr)          # forward hook, none registered
        # open catalog: compute + sensing present WITH manifests; camera omitted
        self.assertEqual(self._names(resp), ["compute", "sensing"])
        self.assertEqual(hdr["catalog_count"], 2)
        self.assertEqual(hdr["catalog_count"], len(resp["catalog"]))
        by = {c["name"]: c for c in resp["catalog"]}
        self.assertEqual(by["compute"]["tier"], "open")
        self.assertIsNotNone(by["compute"]["manifest"])
        self.assertEqual(by["compute"]["manifest"]["consent_tier"], "open")
        self.assertIn("reading", by["compute"]["manifest"])

    def test_sensitive_and_intervention_omitted_for_unauthorized(self):
        # A sensitive diagnostic + a mutating intervention — both needs_approval.
        diag = self.dev.attach_diagnostic("service_health", "nginx.service")["name"]
        intv = self.dev.attach_intervention("process_release", "/tmp/cat_node")["name"]
        resp = self._describe()
        names = self._names(resp)
        # camera (sensitive), the diagnostic (sensitive) and the intervention are
        # ABSENT — not name-only. An unauthorized agent cannot even tell they exist.
        self.assertNotIn("camera", names)
        self.assertNotIn(diag, names)
        self.assertNotIn(intv, names)
        self.assertEqual(names, ["compute", "sensing"])
        self.assertEqual(resp["node"]["catalog_count"], 2)
        # ...and absent from the world-readable names-record too.
        desc = self.dev._node_descriptor()
        self.assertNotIn("camera", desc["capability_names"])
        self.assertNotIn(diag, desc["capability_names"])
        self.assertNotIn(intv, desc["capability_names"])
        self.assertEqual(sorted(desc["capability_names"]), ["compute", "sensing"])

    def test_owner_allow_reveals_in_both_surfaces(self):
        self.assertNotIn("camera", self._names(self._describe()))
        self.dev.policy.allow("camera")                # owner opens it for binding
        # describe_node catalog now lists camera (with its sensitive manifest)...
        resp = self._describe()
        self.assertIn("camera", self._names(resp))
        cam = next(c for c in resp["catalog"] if c["name"] == "camera")
        self.assertEqual(cam["tier"], "sensitive")
        self.assertIsNotNone(cam["manifest"])
        # ...and the names-record lists it too — one predicate, both surfaces.
        desc = self.dev._node_descriptor()
        self.assertIn("camera", desc["capability_names"])

    def test_one_predicate_visibility_tracks_policy_check(self):
        # A cap's catalog visibility flips EXACTLY when policy.check flips — proving
        # there is no second/parallel visibility rule.
        for _ in range(3):
            self.dev.policy.allow("camera")
            self.assertEqual(self.dev.policy.check("camera", "a", is_remote=True), "allow")
            self.assertIn("camera", self._names(self._describe()))
            self.assertIn("camera", self.dev._node_descriptor()["capability_names"])

            self.dev.policy.require_approval("camera")
            self.assertEqual(self.dev.policy.check("camera", "a", is_remote=True), "needs_approval")
            self.assertNotIn("camera", self._names(self._describe()))
            self.assertNotIn("camera", self.dev._node_descriptor()["capability_names"])

            self.dev.policy.deny("camera")
            self.assertNotIn("camera", self._names(self._describe()))

    def test_describe_node_never_prompts_owner(self):
        # A describe is a READ: it must never invoke either approval callback,
        # however many times it is called and whatever sensitive caps exist.
        calls = {"policy": 0, "intervention": 0}

        def policy_cb(resource, agent_id):
            calls["policy"] += 1
            return True

        def intv_cb(plan, agent_id):
            calls["intervention"] += 1
            return True

        self.dev.attach_diagnostic("service_health", "nginx.service")
        self.dev.attach_intervention("process_release", "/tmp/cat_node2")
        self.dev.policy.set_approval_callback(policy_cb)
        self.dev.set_intervention_approval_callback(intv_cb)

        for _ in range(25):
            self.dev._on_message({"type": "describe_node", "from_node": "scanner"})
            self.dev._node_descriptor()
        self.assertEqual(calls["policy"], 0)
        self.assertEqual(calls["intervention"], 0)

    def test_catalog_reflects_live_registry(self):
        # A capability the owner opens at runtime appears; removing it (detach)
        # drops it — the catalog reads the LIVE registry, not a snapshot.
        cap = self.dev.attach_diagnostic("service_health", "cron.service")["name"]
        self.dev.policy.set_approval_callback(lambda r, a: True)  # not consulted here
        self.dev.policy.allow(cap)                     # owner opens the diagnostic
        self.assertIn(cap, self._names(self._describe()))
        self.assertIn(cap, self.dev._node_descriptor()["capability_names"])
        # An unpublished/never-registered name is absent.
        self.assertNotIn("no_such_capability", self._names(self._describe()))
        # Detach → gone from both surfaces.
        self.dev.detach_diagnostic(cap)
        self.assertNotIn(cap, self._names(self._describe()))
        self.assertNotIn(cap, self.dev._node_descriptor()["capability_names"])

    def test_response_is_host_signed_and_tamper_rejected(self):
        resp = self._describe()
        self.assertTrue(signing.is_signed(resp))
        pins = crypto.PinStore(path=crypto.d2a_home() / "pins-verify-cat.json")
        # A pristine response verifies against the host node_id.
        self.assertIsNone(signing.verify_message(resp, self.dev.node_id, pins))
        # Tamper the catalog after signing → signature no longer covers it.
        tampered = dict(resp)
        tampered["catalog"] = resp["catalog"] + [
            {"name": "camera", "tier": "sensitive", "tags": [], "manifest": None}
        ]
        pins2 = crypto.PinStore(path=crypto.d2a_home() / "pins-verify-cat2.json")
        self.assertEqual(signing.verify_message(tampered, self.dev.node_id, pins2),
                         signing.ERR_BAD_SIG)

    def test_names_record_size_discipline_and_truncation(self):
        # Force a tiny ceiling and prove the descriptor truncates + flags it while
        # staying within the byte budget.
        many = [f"cap_{i}" for i in range(10)]
        dev = DeviceRuntime(name=_uniq("bigdev"), capability_override=many,
                            transport=LANSwarm(node_id="unused"))
        # every cap_* is unknown → open? No: unknown resource sensitivity defaults
        # to sensitive. Open them so they are disclosable, exercising the budget.
        for n in many:
            dev.policy.allow(n)

        import json
        orig_names, orig_bytes = device_runtime.MAX_DESCRIPTOR_NAMES, device_runtime.MAX_DESCRIPTOR_BYTES
        try:
            # Count ceiling: 4 names, flagged truncated.
            device_runtime.MAX_DESCRIPTOR_NAMES = 4
            desc = dev._node_descriptor()
            self.assertEqual(len(desc["capability_names"]), 4)
            self.assertTrue(desc["truncated"])
            # Byte ceiling: measure the irreducible floor (signature + keys, zero
            # names), then set a budget a little above it so truncation must trim
            # the name list to fit — but the floor itself is always achievable.
            device_runtime.MAX_DESCRIPTOR_NAMES = 0
            floor = len(json.dumps(dev._node_descriptor(), default=str).encode())
            device_runtime.MAX_DESCRIPTOR_NAMES = 999
            device_runtime.MAX_DESCRIPTOR_BYTES = floor + 40
            desc2 = dev._node_descriptor()
            size = len(json.dumps(desc2, default=str).encode())
            self.assertLessEqual(size, floor + 40)
            self.assertTrue(desc2["truncated"])
            self.assertLess(len(desc2["capability_names"]), 10)
        finally:
            device_runtime.MAX_DESCRIPTOR_NAMES = orig_names
            device_runtime.MAX_DESCRIPTOR_BYTES = orig_bytes

    def test_untruncated_descriptor_within_budget(self):
        desc = self.dev._node_descriptor()
        self.assertFalse(desc["truncated"])
        self.assertTrue(desc.get("node_descriptor"))
        import json
        self.assertLessEqual(
            len(json.dumps(desc, default=str).encode()),
            device_runtime.MAX_DESCRIPTOR_BYTES,
        )


# ── 2. wire: describe_node + enumeration over a real transport ───────────────────

class NodeCatalogWireMixin:
    def setUp(self):
        self.devices: list = []
        self.agents: list = []
        self._setup_transport()

    def tearDown(self):
        for a in self.agents:
            try: a.stop()
            except Exception: pass
        for d in self.devices:
            try: d.stop_swarm()
            except Exception: pass
        self._teardown_transport()

    def test_describe_node_over_wire_verified(self):
        d = self.make_device(_uniq("wdev"), ["compute", "sensing", "camera"])
        a = self.make_agent(_uniq("wag"))
        self._discover(a, d, "compute")
        resp = a.describe_node(d.node_id)
        self.assertTrue(resp.get("verified"))
        self.assertEqual(resp["provider_node_id"], d.node_id)
        self.assertEqual(resp["node"]["node_id"], d.node_id)
        self.assertEqual(resp["node"]["protocol_version"], PROTOCOL_VERSION)
        names = sorted(c["name"] for c in resp["catalog"])
        self.assertEqual(names, ["compute", "sensing"])   # camera omitted
        # composes with describe(name): same manifest for a single cap
        man = a.describe("compute", node_id=d.node_id)
        cat_man = next(c["manifest"] for c in resp["catalog"] if c["name"] == "compute")
        self.assertEqual(man, cat_man)

    def test_enumerate_node_zero_prior_knowledge(self):
        d = self.make_device(_uniq("wdev2"), ["compute", "sensing"])
        a = self.make_agent(_uniq("wag2"))
        self._discover(a, d, "compute")
        names = a.node_capabilities(d.node_id)
        self.assertIn("compute", names)
        self.assertIn("sensing", names)


class TestNodeCatalogWireLAN(NodeCatalogWireMixin, unittest.TestCase):
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
        # Same-host UDP broadcast doesn't loop back — seed records + peer directly.
        ip, port = device.swarm.address
        now = time.time()
        with agent.swarm._lock:
            for c in device.advertise():
                agent.swarm.records[(device.node_id, c.name)] = {
                    "node_id": device.node_id, "name": c.name,
                    "tags": list(c.tags), "live_state": dict(c.live_state),
                    "public_key": device.public_key, "address": [ip, port],
                    "device_class": device.device_class,
                    "manifest": device._cap_manifest(c), "ts": now,
                }
        agent.swarm.add_known_peer(device.node_id, ip, port)


class TestNodeCatalogWireDHT(NodeCatalogWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="catalog-bootstrap", udp_port=free_udp_port(), ttl=30)
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

    def test_names_record_signed_and_omits_sensitive(self):
        # The DHT node:<id> descriptor is discoverable with zero prior knowledge,
        # verifies + TOFU-pins, and omits the sensitive capability's name.
        d = self.make_device(_uniq("dhtdev"), ["compute", "sensing", "camera"])
        a = self.make_agent(_uniq("dhtag"))
        time.sleep(0.5)
        raw = a.swarm.fetch_node_descriptor(d.node_id)
        self.assertIsNotNone(raw)
        self.assertTrue(raw.get("node_descriptor"))
        self.assertIsNone(signing.verify_record(raw, a.pins))   # signed + pinned
        names = a.node_capabilities(d.node_id)
        self.assertEqual(sorted(names), ["compute", "sensing"])
        self.assertNotIn("camera", names)

    def test_tampered_names_record_rejected(self):
        d = self.make_device(_uniq("dhtdev2"), ["compute", "sensing"])
        a = self.make_agent(_uniq("dhtag2"))
        time.sleep(0.5)
        raw = a.swarm.fetch_node_descriptor(d.node_id)
        self.assertIsNotNone(raw)
        raw["capability_names"] = list(raw["capability_names"]) + ["camera"]
        # verify_record over the tampered descriptor fails (fresh pin store).
        pins = crypto.PinStore(path=crypto.d2a_home() / "pins-tamper-desc.json")
        self.assertIsNotNone(signing.verify_record(raw, pins))


if __name__ == "__main__":
    unittest.main(verbosity=2)
