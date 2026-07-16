"""
tests/test_boundary.py — PHASE 11: capability boundaries (v1.11).

The MCP "roots" concept, adapted: an intervention manifest may declare a
BOUNDARY — the operational lane of targets/params it may EVER act on — and the
device enforces it BEFORE preflight and BEFORE both consent gates, so an
out-of-boundary plan is refused structurally regardless of any approval, and
the owner is never prompted. Boundary is a pre-filter, NOT a replacement for
consent: in-boundary still requires the full per-plan owner approval.

Covers (the full Phase 11 list):
  - vocabulary: glob ("match") / set ("in") / range ("range") each validated +
    enforced; exactly one match type; typed against the paramspec
  - boundary-on-nonexistent-param rejected at publish time
  - boundary rejected on non-intervention tiers (no unenforced decoration)
  - attach-time lane check: a fixed target outside its own declared boundary
    can never be published
  - out-of-boundary propose refused with OUT_OF_BOUNDARY, the owner callback
    NOT invoked (asserted), nothing mutated, and the attempt AUDITED
  - a constrained param omitted from the plan is a violation (no dodging a
    signal allow-list by riding the executor default)
  - in-boundary passes THROUGH to the still-required consent gate (both hold)
  - absent boundary → behavior unchanged (compat)
  - boundary visible in describe_node for an authorized agent
  - both transports (LAN + DHT)

Run:  python3 -m unittest tests.test_boundary -v
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a import errors
from d2a import boundary as _boundary
from d2a import manifest as _manifest
from d2a.stream_source import DeviceNodeHealthSource
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


def _release_plan(pid: int, signal: str | None = None) -> dict:
    p = {"action": "release", "params": {"pid": pid},
         "evidence": {"field": "holder_count", "reading": {"holder_count": 1}},
         "expected": "node released (0 holders)",
         "verify": {"diagnostic": "device_node_health",
                    "condition": {"field": "holder_count", "op": "eq", "value": 0}},
         "reversible": False, "reversible_how": "", "reversible_ack": True}
    if signal is not None:
        p["params"]["signal"] = signal
    return p


# ── 1. vocabulary: validate_boundary (publish-time) ──────────────────────────────

class TestBoundaryVocabulary(unittest.TestCase):
    def _man(self):
        return _manifest.intervention_manifest("process_release", "/dev/video0")

    def test_all_three_match_types_validate(self):
        b = {"target": {"match": "/dev/video*"},
             "pid":    {"range": [300, 99999]},
             "signal": {"in": ["TERM", "HUP"]}}
        self.assertEqual(_boundary.validate_boundary(b, self._man()), b)
        # and through the manifest composer — it rides the validated manifest
        m = _manifest.intervention_manifest("process_release", "/dev/video0", b)
        self.assertEqual(m["boundary"], b)

    def test_nonexistent_param_rejected_at_publish(self):
        # service_intervene's actions take NO params — 'unit' is not a param,
        # the lane for the unit is the reserved 'target' key.
        with self.assertRaises(_manifest.ManifestError) as cm:
            _manifest.intervention_manifest("service_intervene", "d2a-x.service",
                                            {"unit": {"match": "d2a-*"}})
        self.assertIn("unit", str(cm.exception))

    def test_target_reserved_key_valid_on_paramless_family(self):
        m = _manifest.intervention_manifest("service_intervene", "d2a-x.service",
                                            {"target": {"match": "d2a-*"}})
        self.assertEqual(m["boundary"], {"target": {"match": "d2a-*"}})

    def test_exactly_one_match_type_required(self):
        for bad in ({"pid": {}},                                     # zero
                    {"pid": {"range": [1, 2], "in": [1]}}):          # two
            with self.assertRaises(_boundary.BoundaryError):
                _boundary.validate_boundary(bad, self._man())

    def test_empty_and_malformed_boundary_rejected(self):
        for bad in ({}, "d2a-*", {"pid": "1-2"}, {"pid": {"glob": "x"}}):
            with self.assertRaises(_boundary.BoundaryError):
                _boundary.validate_boundary(bad, self._man())

    def test_constraint_typed_against_paramspec(self):
        man = self._man()
        # range on a string param
        with self.assertRaises(_boundary.BoundaryError):
            _boundary.validate_boundary({"signal": {"range": [1, 2]}}, man)
        # match (glob) on a number param
        with self.assertRaises(_boundary.BoundaryError):
            _boundary.validate_boundary({"pid": {"match": "12*"}}, man)
        # in-set items must match the param type
        with self.assertRaises(_boundary.BoundaryError):
            _boundary.validate_boundary({"pid": {"in": ["300"]}}, man)
        # malformed range
        with self.assertRaises(_boundary.BoundaryError):
            _boundary.validate_boundary({"pid": {"range": [99, 1]}}, man)

    def test_boundary_rejected_on_non_intervention_tier(self):
        # A declared boundary nobody enforces would look like protection — the
        # vocabulary is generic, but v1 rejects it outside the intervention tier.
        with self.assertRaises(_manifest.ManifestError) as cm:
            _manifest.validate_manifest(
                {"description": "x", "consent_tier": "open",
                 "boundary": {"target": {"match": "a*"}}}, "open")
        self.assertIn("intervention", str(cm.exception))


# ── 2. enforcement semantics: check() (the generic reusable half) ─────────────────

class TestBoundaryCheck(unittest.TestCase):
    def test_set_glob_range_each_enforced(self):
        b = {"target": {"match": "/dev/video*"},
             "pid":    {"range": [300, 99999]},
             "signal": {"in": ["TERM", "HUP"]}}
        ok, why = _boundary.check(b, {"target": "/dev/video0", "pid": 4242, "signal": "TERM"})
        self.assertTrue(ok, why)
        for vals, frag in (
            ({"target": "/dev/sda",    "pid": 4242, "signal": "TERM"}, "target"),   # glob
            ({"target": "/dev/video0", "pid": 5,    "signal": "TERM"}, "range"),    # range
            ({"target": "/dev/video0", "pid": 4242, "signal": "KILL"}, "signal"),   # set
        ):
            ok, why = _boundary.check(b, vals)
            self.assertFalse(ok)
            self.assertIn(frag, why)

    def test_constrained_param_omitted_is_a_violation(self):
        # Deny-by-default shape: an agent must not dodge {"signal": {"in": [...]}}
        # by omitting `signal` and riding the executor's default.
        ok, why = _boundary.check({"signal": {"in": ["TERM"]}}, {"target": "t", "pid": 1})
        self.assertFalse(ok)
        self.assertIn("signal", why)

    def test_absent_boundary_passes_everything(self):
        for b in (None, {}):
            self.assertEqual(_boundary.check(b, {"anything": "goes"}), (True, ""))


# ── 3. attach-time lane check (publish gate, no transport) ────────────────────────

class TestAttachBoundary(unittest.TestCase):
    def setUp(self):
        self.d = DeviceRuntime(name="bnd-attach", capability_override=["compute"])

    def tearDown(self):
        try: self.d.stop_swarm()
        except Exception: pass

    def test_invalid_boundary_refuses_attach(self):
        r = self.d.attach_intervention("process_release", "/tmp/x_node",
                                       boundary={"nope": {"in": ["x"]}})
        self.assertEqual(r.get("error"), "invalid_boundary")
        self.assertNotIn("name", r)

    def test_fixed_target_outside_own_lane_refuses_attach(self):
        # A capability pointing outside its own declared boundary can never be
        # published — the lane check bites at attach for target constraints.
        r = self.d.attach_intervention("service_intervene", "nginx.service",
                                       boundary={"target": {"match": "d2a-*"}})
        self.assertEqual(r.get("error"), "target_out_of_boundary")
        self.assertNotIn("name", r)

    def test_valid_boundary_rides_the_signed_manifest(self):
        b = {"target": {"match": "/tmp/bnd_*"}, "pid": {"range": [1, 99999]}}
        r = self.d.attach_intervention("process_release", "/tmp/bnd_node", boundary=b)
        cap = self.d.capabilities[r["name"]]
        self.assertEqual(cap.manifest["boundary"], b)
        # and it is inside the Ed25519-signed published record (authenticated lane)
        rec = self.d._capability_record(cap, "127.0.0.1", 0)
        self.assertEqual(rec["manifest"]["boundary"], b)

    def test_absent_boundary_attach_unchanged(self):
        r = self.d.attach_intervention("process_release", "/tmp/free_node")
        self.assertIn("name", r)
        self.assertNotIn("boundary", self.d.capabilities[r["name"]].manifest)


# ── 4. wire: enforcement BEFORE consent, audit, describe_node, both transports ────

class BoundaryWireMixin:
    def setUp(self):
        self.devices, self.agents, self.tmpfiles, self.procs = [], [], [], []
        self._setup_transport()

    def tearDown(self):
        for p in self.procs:
            try: p.kill()
            except Exception: pass
        for a in self.agents:
            try: a.stop()
            except Exception: pass
        for d in self.devices:
            try: d.stop_swarm()
            except Exception: pass
        for f in self.tmpfiles:
            try: os.remove(f)
            except OSError: pass
        self._teardown_transport()
        time.sleep(0.05)

    def _settle(self):
        pass

    def _discover(self, agent, device, cap):
        raise NotImplementedError

    def _attach(self, device, family, target, **kw):
        name = device.attach_intervention(family, target, **kw)["name"]
        self._settle()
        return name

    def _bind(self, agent, device, cap):
        self._discover(agent, device, cap)
        return agent.bind_remote_to(device.node_id, cap)

    def _fixture_holder(self, prefix="bnd_node_"):
        """A temp 'device node' + a child process that keeps it open."""
        fd, node = tempfile.mkstemp(prefix=prefix); os.close(fd)
        self.tmpfiles.append(node)
        child = subprocess.Popen(["python3", "-c",
                                  f"f=open({node!r},'rb'); import time; time.sleep(120)"])
        self.procs.append(child)
        time.sleep(0.4)
        return node, child

    # out-of-boundary → refused BEFORE consent: distinct code, owner callback
    # NEVER invoked, nothing mutated, attempt AUDITED.
    def test_out_of_boundary_refused_before_consent_and_audited(self):
        d = self.make_device("oob")
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node,
                           boundary={"pid": {"range": [1, 300]}})   # child.pid >> 300
        d.policy.set_approval_callback(lambda res, aid: True)        # bind gate open
        prompts = []                                                 # the assert flag
        d.set_intervention_approval_callback(
            lambda plan, aid: (prompts.append(plan), True)[1])       # WOULD approve
        a = self.make_agent("oobag")
        r = self._bind(a, d, cap)
        self.assertTrue(r.get("verified"), r)

        resp = a.propose_intervention(r, _release_plan(child.pid))
        self.assertEqual(resp["status"], "out_of_boundary", resp)
        self.assertEqual(resp.get("code"), errors.OUT_OF_BOUNDARY)
        self.assertFalse(resp["approved"])
        self.assertFalse(resp["executed"])
        self.assertEqual(prompts, [],
                         "owner callback must NEVER be invoked for an out-of-boundary plan")
        self.assertIsNone(child.poll(), "nothing mutated — the child still runs")
        # the ATTEMPT is audited (a well-formed plan aimed outside the lane is
        # exactly the probe an audit log exists to capture)
        head = d._audit_log().head()
        self.assertEqual(head["result_status"], "out_of_boundary")
        self.assertFalse(head["approved"])
        self.assertFalse(head["executed"])
        self.assertEqual(head["plan_hash"], resp["plan_hash"])
        self.assertEqual(resp["audit_seq"], head["seq"])

    # set-type lane on `signal`: a forbidden signal AND a dodged (omitted) signal
    # are both out-of-boundary — a constrained param is effectively required.
    def test_signal_allowlist_enforced_and_not_dodgeable(self):
        d = self.make_device("sig")
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node,
                           boundary={"signal": {"in": ["TERM", "HUP"]}})
        d.policy.set_approval_callback(lambda res, aid: True)
        prompts = []
        d.set_intervention_approval_callback(
            lambda plan, aid: (prompts.append(plan), True)[1])
        a = self.make_agent("sigag")
        r = self._bind(a, d, cap)

        kill = a.propose_intervention(r, _release_plan(child.pid, signal="KILL"))
        self.assertEqual(kill["status"], "out_of_boundary")
        dodge = a.propose_intervention(r, _release_plan(child.pid))   # signal omitted
        self.assertEqual(dodge["status"], "out_of_boundary")
        self.assertEqual(prompts, [], "no owner prompt on either attempt")
        self.assertIsNone(child.poll(), "the child was never signalled")

    # in-boundary is NOT approval: it passes THROUGH to the still-required
    # consent gate. Both must hold.
    def test_in_boundary_still_requires_consent_then_executes(self):
        d = self.make_device("both")
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node,
                           boundary={"pid": {"range": [1, 4194304]},
                                     "signal": {"in": ["TERM"]}})
        d.policy.set_approval_callback(lambda res, aid: True)
        a = self.make_agent("bothag")
        r = self._bind(a, d, cap)
        plan = _release_plan(child.pid, signal="TERM")

        # no per-plan callback → the consent gate still denies an IN-boundary plan
        denied = a.propose_intervention(r, plan)
        self.assertEqual(denied["status"], "denied")
        self.assertEqual(denied.get("code"), errors.APPROVAL_REQUIRED)
        self.assertIsNone(child.poll(), "in-boundary alone must not execute")

        # owner approves → the same plan executes and the device-run verify passes
        d.set_intervention_approval_callback(lambda plan, aid: True)
        done = a.propose_intervention(r, plan)
        self.assertEqual(done["status"], "executed", done)
        self.assertTrue(done["verify"]["passed"])
        self.assertEqual(DeviceNodeHealthSource(node).read()["holder_count"], 0)

    # compat rule: an ABSENT boundary changes nothing — the plan reaches the
    # consent gate exactly as in v1.10.
    def test_absent_boundary_unchanged(self):
        d = self.make_device("compat")
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node)               # no boundary
        d.policy.set_approval_callback(lambda res, aid: True)
        a = self.make_agent("compatag")
        r = self._bind(a, d, cap)
        resp = a.propose_intervention(r, _release_plan(child.pid))
        self.assertEqual(resp["status"], "denied")                    # consent gate,
        self.assertEqual(resp.get("code"), errors.APPROVAL_REQUIRED)  # not boundary
        head = d._audit_log().head()
        self.assertEqual(head["result_status"], "denied")

    # an AUTHORIZED agent sees the lane: the boundary rides the manifest into
    # the describe_node catalog (one _cap_manifest expression, no extra surface).
    def test_boundary_visible_in_describe_node_for_authorized_agent(self):
        d = self.make_device("lane")
        node, _child = self._fixture_holder()
        b = {"pid": {"range": [1, 4194304]}, "signal": {"in": ["TERM"]}}
        cap = self._attach(d, "process_release", node, boundary=b)
        d.policy.allow(cap)                       # owner opens it → disclosable
        self._settle()
        a = self.make_agent("laneag")
        self._discover(a, d, cap)
        resp = a.describe_node(d.node_id)
        self.assertTrue(resp.get("verified"), resp)
        entry = next(c for c in resp["catalog"] if c["name"] == cap)
        self.assertEqual(entry["tier"], "intervention")
        self.assertEqual(entry["manifest"]["boundary"], b)


# ── LAN concrete ─────────────────────────────────────────────────────────────────

class TestBoundaryLAN(BoundaryWireMixin, unittest.TestCase):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass

    def make_device(self, name):
        d = DeviceRuntime(name=f"lan-bnd-{name}", capability_override=["compute"],
                          lease_ttl=120)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=f"lan-bnd-{name}", auto_renew=False)
        a.start()
        self.agents.append(a)
        return a

    def _discover(self, agent, device, cap):
        ip, port = device.swarm.address
        now = time.time()
        with agent.swarm._lock:
            for c in device.advertise():
                rec = {"node_id": device.node_id, "name": c.name, "tags": list(c.tags),
                       "live_state": dict(c.live_state), "public_key": device.public_key,
                       "address": [ip, port], "device_class": device.device_class, "ts": now}
                man = device._capability_record(c, ip, port).get("manifest")
                if man is not None:
                    rec["manifest"] = man
                agent.swarm.records[(device.node_id, c.name)] = rec
        agent.swarm.add_known_peer(device.node_id, ip, port)


# ── DHT concrete ─────────────────────────────────────────────────────────────────

class TestBoundaryDHT(BoundaryWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="bnd-bootstrap", udp_port=free_udp_port(), ttl=30)
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

    def make_device(self, name):
        d = DeviceRuntime(name=f"dht-bnd-{name}", capability_override=["compute"],
                          lease_ttl=120)
        self._attach_dht(d)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=f"dht-bnd-{name}", auto_renew=False)
        self._attach_dht(a)
        a.start()
        self.agents.append(a)
        time.sleep(0.3)
        return a

    def _settle(self):
        time.sleep(0.4)

    def _discover(self, agent, device, cap):
        agent.find_capability(cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
