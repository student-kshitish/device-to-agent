"""
tests/test_publish_derived.py — Phase 3 of CASE 4: derived capabilities ON-WIRE.

A capability that no device provides is SYNTHESISED locally (Phase 2), then
PUBLISHED so other agents can discover, bind, read, and subscribe to it like any
real capability — for ANY device class. This closes derivation protocol gaps 1
(per-field cadence) and 3 (derived provenance on-wire); PROTOCOL_VERSION -> 1.5,
additive.

Covered here:
  * manifest vocabulary v1.5: the four derived-provenance keys (conditional
    required/forbidden) and optional per-field hz (contract-checker honours it).
  * end-to-end, generic + BOTH transports: agent A derives -> publishes -> agent B
    discovers with zero prior knowledge, reads provenance from the manifest, binds,
    pulls readings, subscribes a condition on a DERIVED field, gets an edge event.
  * consent: a sensitive derived capability denies an unapproved consumer at the
    bind gate (no bypass through the publish path).
  * lifecycle: required-input death -> unpublish + consumer gets a lease-loss with
    the DISTINCT derived_input_failed code; publisher graceful shutdown ->
    consumer gets device_shutdown.
  * the whole four-recipe pack: registry-load + deterministic dry-run, and the two
    real-hardware recipes execute live against shipped capabilities.

Run:  python3 -m unittest tests.test_publish_derived -v
"""

import os
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a import manifest as M
from d2a import errors
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from d2a_derive import (
    Registry, Planner, TrustStore, DerivedCapability, check_input_against_provider,
)
from d2a_derive.demo_scaffolding import register_demo_odometry, OdometrySource
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent
from tests._env import use_tmp_home, restore_home
import json as _json

_REF = Path(__file__).resolve().parent.parent / "d2a_derive" / "reference_recipes"
TTL = 3


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


def demo_trust() -> TrustStore:
    demo = _json.loads((_REF / "DEMO_reference_author.json").read_text())
    t = TrustStore()
    t.add(demo["public_key"], "demo")
    return t


# ── manifest vocabulary v1.5 (no transport) ───────────────────────────────────

class TestDerivedVocabulary(unittest.TestCase):
    def _base(self):
        return {"description": "d", "reading": {}, "consent_tier": "open", "streaming": False}

    def test_derived_with_all_four_keys_validates(self):
        m = {**self._base(), "derived": True, "recipe": "r",
             "fidelity": "coarse", "cannot_detect": ["x", "y"]}
        out = M.validate_manifest(m, "open")
        self.assertTrue(out["derived"])
        self.assertEqual(out["recipe"], "r")

    def test_derived_missing_fidelity_rejected(self):
        m = {**self._base(), "derived": True, "recipe": "r", "cannot_detect": ["x"]}
        with self.assertRaises(M.ManifestError):
            M.validate_manifest(m, "open")

    def test_derived_missing_cannot_detect_rejected(self):
        m = {**self._base(), "derived": True, "recipe": "r", "fidelity": "f"}
        with self.assertRaises(M.ManifestError):
            M.validate_manifest(m, "open")

    def test_non_derived_carrying_provenance_rejected(self):
        for extra in ({"recipe": "r"}, {"fidelity": "f"}, {"cannot_detect": ["x"]}):
            with self.assertRaises(M.ManifestError):
                M.validate_manifest({**self._base(), **extra}, "open")

    def test_per_field_hz_validates_and_rejects_bad(self):
        ok = {**self._base(), "reading": {"f": {"type": "number", "hz": 2.5}}}
        M.validate_manifest(ok, "open")
        for bad in (-1, 0, True, "fast"):
            with self.assertRaises(M.ManifestError):
                M.validate_manifest(
                    {**self._base(), "reading": {"f": {"type": "number", "hz": bad}}}, "open")

    def test_contract_checker_honours_declared_hz(self):
        req = {"capability_hint": "x",
               "fields": {"f": {"type": "number", "min_hz": 5.0}}}
        slow = {"reading": {"f": {"type": "number", "hz": 1.0}}}
        fast = {"reading": {"f": {"type": "number", "hz": 10.0}}}
        nohz = {"reading": {"f": {"type": "number"}}}
        self.assertFalse(check_input_against_provider(req, slow, {})[0])  # 1 Hz < 5 Hz
        self.assertTrue(check_input_against_provider(req, fast, {})[0])   # 10 Hz >= 5 Hz
        self.assertTrue(check_input_against_provider(req, nohz, {})[0])   # absent → clamp 10 ≥ 5


# ── recipe pack: load + dry-run + real-hardware execution ─────────────────────

class TestRecipePack(unittest.TestCase):
    def setUp(self):
        self.reg = Registry(recipes_dir=_REF, trust=demo_trust())

    def test_all_pack_recipes_admitted(self):
        # four device-class substitutes + the Phase-4 activity_summary chain recipe.
        self.assertEqual(self.reg.rejected, [])
        self.assertEqual(sorted(r.provided_name for r in self.reg.loaded),
                         ["activity_summary", "ambient_temp", "free_space_map",
                          "load_trend", "presence"])

    def test_dry_run_deterministic_for_all(self):
        for lr in self.reg.loaded:
            self.assertTrue(lr.dry_run.ok, f"{lr.recipe_name}: {lr.dry_run.reason}")
            # determinism is enforced at admission (run twice); re-affirm the sample
            # is a complete, non-empty reading.
            self.assertTrue(lr.dry_run.sample_output, lr.recipe_name)

    def test_real_hardware_recipes_execute_live(self):
        # presence + load_trend bind REAL shipped capabilities — no scaffolding.
        dev = DeviceRuntime(name="pack-dev", capability_override=["compute", "sensing"],
                            lease_ttl=8)
        dev.start_swarm()
        ag = RemoteAgent(name="pack-ag")
        ag.start()
        ip, port = dev.swarm.address
        with ag.swarm._lock:
            for c in dev.advertise():
                ag.swarm.records[(dev.node_id, c.name)] = dev._capability_record(c, ip, port)
        ag.swarm.add_known_peer(dev.node_id, ip, port)
        pl = Planner(self.reg, discover=lambda n: [
            dict(r) for (nid, nm), r in ag.swarm.records.items()
            if nm == n and isinstance(r.get("manifest"), dict)])
        try:
            for cap, fields in (("presence", {"in_use", "activity_level", "score"}),
                                ("load_trend", {"sustained_load", "trend", "smoothed_temp_c"})):
                res = pl.need(cap)
                self.assertEqual(res.outcome, "derived", cap)
                dc = DerivedCapability(res.plan, ag).start()
                try:
                    end = time.time() + 8
                    while time.time() < end and dc.reading() is None:
                        time.sleep(0.2)
                    self.assertIsNotNone(dc.reading(), f"{cap} produced no live reading")
                    self.assertEqual(set(dc.reading()), fields, cap)
                finally:
                    dc.close()
        finally:
            ag.stop()
            dev.stop_swarm()


# ── shared publish/consume harness ────────────────────────────────────────────

class PublishMixin:
    def setUp(self):
        self.devices, self.agents, self.dcs = [], [], []
        self.reg = Registry(recipes_dir=_REF, trust=demo_trust())
        self._setup_transport()

    def tearDown(self):
        for dc in self.dcs:
            try: dc.close()
            except Exception: pass
        for a in self.agents:
            try: a.stop()
            except Exception: pass
        for d in self.devices:
            try: d.stop_swarm()
            except Exception: pass
        self._teardown_transport()
        time.sleep(0.05)

    def make_device(self, name, caps=("compute",), odo=False, approval=None):
        d = DeviceRuntime(name=name, capability_override=list(caps), lease_ttl=TTL,
                          approval_callback=approval)
        self._attach(d)
        if odo:
            register_demo_odometry(d, source=OdometrySource(step_m=0.6, turn=0.7))
        d.start_swarm()
        self.devices.append(d)
        self._after_device()
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=name, auto_renew=True)
        self._attach(a)
        a.start()
        self.agents.append(a)
        self._after_agent()
        return a

    def planner(self, agent, *devices):
        for d in devices:
            self._seed(agent, d)

        def discover(name):
            with agent.swarm._lock:
                return [dict(r) for (nid, nm), r in agent.swarm.records.items()
                        if nm == name and isinstance(r.get("manifest"), dict)]
        return Planner(self.reg, discover=discover)

    def derive(self, plan, agent, **kw):
        dc = DerivedCapability(plan, agent, **kw)
        self.dcs.append(dc)
        return dc.start()

    def wait_until(self, pred, timeout=10.0, interval=0.1):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(interval)
        return pred()


class PublishLAN(PublishMixin):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass
    def _attach(self, obj): pass
    def _after_device(self): pass
    def _after_agent(self): pass

    def _seed(self, agent, device):
        ip, port = device.swarm.address
        with agent.swarm._lock:
            for c in device.advertise():
                agent.swarm.records[(device.node_id, c.name)] = \
                    device._capability_record(c, ip, port)
        agent.swarm.add_known_peer(device.node_id, ip, port)


class PublishDHT(PublishMixin):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="pub-bootstrap", udp_port=free_udp_port(), ttl=30)
        self.boot.start()
        self.boot_addr = ("127.0.0.1", self.boot.udp_port)

    def _teardown_transport(self):
        self.boot.stop()

    def _attach(self, obj):
        node_id = getattr(obj, "node_id", None) or obj.agent_id
        try: obj.swarm._tcp_srv.close()
        except Exception: pass
        obj.swarm = DHTSwarm(node_id=node_id, dht_port=free_udp_port(),
                             bootstrap=self.boot_addr, ttl=30)

    def _after_device(self): time.sleep(0.4)
    def _after_agent(self): time.sleep(0.3)

    def _seed(self, agent, device):
        for c in device.advertise():
            agent.find_capability(c.name)


# ── end-to-end publish → discover → bind → read → event (both transports) ─────

class PublishE2ETests(PublishMixin):

    def test_derive_publish_discover_bind_read_event(self):
        # Deriver D provides demo_odometry (real bindable input) AND hosts the
        # publish; agent A binds D's odometry and derives free_space_map; D publishes
        # it (sensitive → owner approves this consumer); consumer B, with ZERO prior
        # knowledge beyond discovery, learns the provenance, binds, reads, and gets
        # an edge-fired event on a derived field.
        approvals = {"ok": True}
        D = self.make_device("deriver", caps=("compute",), odo=True,
                             approval=lambda res, aid: approvals["ok"])
        A = self.make_agent("agent-a")
        plA = self.planner(A, D)
        res = plA.need("free_space_map")
        self.assertEqual(res.outcome, "derived")
        dc = self.derive(res.plan, A)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        pub = dc.publish(D)
        self.assertEqual(pub["kind"], "derived")

        # Consumer B — fresh node, discovers the published derived capability.
        B = self.make_agent("consumer-b")
        self._seed(B, D)
        man = B.describe("free_space_map", node_id=D.node_id)
        self.assertIsNotNone(man, "B could not discover the published derived manifest")
        self.assertTrue(man.get("derived"))
        self.assertEqual(man.get("recipe"), "trajectory_free_space_map")
        self.assertTrue(man.get("fidelity"))
        self.assertIn("obstacles", man.get("cannot_detect", []))

        binding = B.bind_remote_to(D.node_id, "free_space_map")
        self.assertTrue(binding.get("verified"), "B failed to bind approved derived cap")

        # pull a reading — carries the derived output + the live derived_state.
        frame = B.request_data(binding, "free_space_map")
        self.assertEqual(frame.get("type"), "reading")
        body = frame.get("frame") or {}
        self.assertIn("free_cells", body)
        self.assertEqual(body.get("derived_state"), "active")

        # subscribe a condition on a DERIVED field; the map grows → free_cells
        # changes → an edge event fires to the remote subscriber.
        events = []
        resp = B.on_event(binding, {"field": "free_cells", "op": "changed"},
                          lambda e: events.append(e), eval_hz=5.0)
        self.assertEqual(resp.get("status"), "subscribed", resp)
        self.assertTrue(self.wait_until(lambda: len(events) >= 1, timeout=10.0),
                        "no edge event on the derived field")
        self.assertIn("reading", events[0])


class TestPublishE2ELAN(PublishE2ETests, PublishLAN, unittest.TestCase):
    pass


class TestPublishE2EDHT(PublishE2ETests, PublishDHT, unittest.TestCase):
    pass


# ── consent + lifecycle (LAN-only logic) ──────────────────────────────────────

class TestPublishConsent(PublishLAN, unittest.TestCase):

    def test_sensitive_derived_denies_unapproved_consumer(self):
        # No approval callback on D → default DENY for a sensitive derived cap.
        D = self.make_device("deriver", caps=("compute",), odo=True)
        A = self.make_agent("agent-a")
        res = self.planner(A, D).need("free_space_map")
        dc = self.derive(res.plan, A)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        dc.publish(D)                                   # sensitive → require_approval

        B = self.make_agent("consumer-b")
        self._seed(B, D)
        binding = B.bind_remote_to(D.node_id, "free_space_map")
        self.assertFalse(binding.get("verified"), "sensitive derived must NOT bind unapproved")
        self.assertEqual(binding.get("status"), "denied")
        self.assertEqual(binding.get("code"), errors.APPROVAL_REQUIRED)


class TestPublishLifecycle(PublishLAN, unittest.TestCase):

    def _setup_pub(self):
        # provider P (demo_odometry) separate from deriver/publisher D, so we can
        # kill the INPUT (P) without stopping the publisher (D).
        self.P = self.make_device("provider", caps=("compute",), odo=True)
        self.D = self.make_device("deriver", caps=("compute",),
                                  approval=lambda r, a: True)   # approve consumers
        self.A = self.make_agent("agent-a")
        res = self.planner(self.A, self.P).need("free_space_map")
        self.assertEqual(res.outcome, "derived")
        self.dc = self.derive(res.plan, self.A,
                              heal_max_attempts=2, heal_shutdown_backoff_s=1.0)
        self.assertTrue(self.wait_until(lambda: self.dc.reading() is not None))
        self.dc.publish(self.D)

        self.B = self.make_agent("consumer-b")
        self._seed(self.B, self.D)
        self.losses = []
        self.B.on_lease_lost = lambda bid, code: self.losses.append(code)
        binding = self.B.bind_remote_to(self.D.node_id, "free_space_map")
        self.assertTrue(binding.get("verified"))
        return binding

    def test_required_input_death_unpublishes_and_signals_consumer(self):
        self._setup_pub()
        # kill the REQUIRED input by stopping its provider P (device_shutdown).
        self.P.stop_swarm()
        # derivation fails → D unpublishes → B gets the DISTINCT death code.
        self.assertTrue(self.wait_until(lambda: self.dc.state == "failed", timeout=8.0),
                        "derivation did not fail on required-input death")
        self.assertTrue(self.wait_until(
            lambda: errors.DERIVED_INPUT_FAILED in self.losses, timeout=6.0),
            f"consumer did not get derived_input_failed (got {self.losses})")
        # the published capability is gone from the deriver.
        self.assertNotIn("free_space_map", self.D.capabilities)

    def test_publisher_graceful_shutdown_signals_device_shutdown(self):
        self._setup_pub()
        # publisher D departs gracefully → consumer B gets device_shutdown (distinct
        # from the derived-input-failure code).
        self.D.stop_swarm()
        self.assertTrue(self.wait_until(
            lambda: errors.DEVICE_SHUTDOWN in self.losses, timeout=6.0),
            f"consumer did not get device_shutdown (got {self.losses})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
