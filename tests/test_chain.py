"""
tests/test_chain.py — Phase 4 of CASE 4: MULTI-HOP DERIVATION CHAINING.

A recipe's `requires` may be satisfied by a DERIVED capability — published by
another agent (on-wire) or planned locally (local chaining) — so derivations stack:
compute → presence → activity_summary. Application layer only; the v1.5 manifest
vocabulary carries provenance through the hops (no protocol change).

Covered here:
  * STRICT PREFERENCE, per tier: real provider > single-hop derived > two-hop chain
    (never chain when a shorter path satisfies).
  * two-hop end-to-end with nested provenance + chain-max tier + cannot_detect
    union asserted field-by-field.
  * cycle rejected (distinct code); depth-3 rejected (distinct code).
  * inner-input death propagates outward (required → failed, optional → degraded).
  * inner PUBLISHED capability's graceful shutdown propagates to the outer.
  * chains run fully-local AND across-the-wire (inner published by a 2nd runtime,
    outer consumes it) on BOTH transports.

Run:  python3 -m unittest tests.test_chain -v
"""

import json
import os
import shutil
import socket
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a import crypto
from d2a import manifest as M
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from d2a_derive import Registry, Planner, TrustStore, DerivedCapability, sign_recipe
from d2a_derive import errors
from d2a_derive.planner import MAX_DERIVATION_DEPTH, Provenance
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent
from tests._env import use_tmp_home, restore_home

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
    demo = json.loads((_REF / "DEMO_reference_author.json").read_text())
    t = TrustStore()
    t.add(demo["public_key"], "demo")
    return t


class _Cap:
    def __init__(self, name):
        self.name = name
        self.live_state = {}


def _prov(node_id, name, manifest):
    return {"node_id": node_id, "name": name, "manifest": manifest}


def _compute_prov(node_id="nC"):
    return _prov(node_id, "compute", M.builtin_manifest(_Cap("compute")))


def _real_presence_prov(node_id="nP"):
    # a hypothetical REAL presence sensor (NOT derived) — used to prove single-hop
    # beats two-hop (the planner must prefer this over deriving presence).
    man = {"description": "real presence sensor", "consent_tier": "sensitive",
           "streaming": False,
           "reading": {"in_use": {"type": "boolean"},
                       "activity_level": {"type": "string"},
                       "score": {"type": "number", "unit": "%"}}}
    return _prov(node_id, "presence", man)


# ── STRICT PREFERENCE ORDER (planner-only) ────────────────────────────────────

class TestPreferenceOrder(unittest.TestCase):
    def setUp(self):
        self.reg = Registry(recipes_dir=_REF, trust=demo_trust())

    def _planner(self, providers):
        return Planner(self.reg, discover=lambda n: providers.get(n, []))

    def test_real_provider_beats_all_derivations(self):
        real = _prov("nAS", "activity_summary",
                     {"description": "real", "consent_tier": "sensitive", "streaming": False,
                      "reading": {"duty_cycle": {"type": "number"}}})
        res = self._planner({"activity_summary": [real],
                             "presence": [_real_presence_prov()],
                             "compute": [_compute_prov()]}).need("activity_summary")
        self.assertEqual(res.outcome, "direct")
        self.assertEqual(res.plan, None)

    def test_single_hop_beats_two_hop(self):
        # a real 'presence' provider exists → derive activity_summary in ONE hop,
        # never chaining down to compute.
        res = self._planner({"presence": [_real_presence_prov("nP")],
                             "compute": [_compute_prov()]}).need("activity_summary")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.depth, 1)
        self.assertIn("provider", res.plan.inputs[0])
        self.assertNotIn("inner_plan", res.plan.inputs[0])
        self.assertEqual(res.plan.inputs[0]["provider"]["node_id"], "nP")

    def test_two_hop_only_when_no_shorter_path(self):
        # only the leaf (compute) is real → must chain compute → presence → summary.
        res = self._planner({"compute": [_compute_prov("nC")]}).need("activity_summary")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.depth, 2)
        self.assertIn("inner_plan", res.plan.inputs[0])


# ── PROVENANCE / TIER / cannot_detect UNION (planner-only) ─────────────────────

class TestChainProvenance(unittest.TestCase):
    def setUp(self):
        self.reg = Registry(recipes_dir=_REF, trust=demo_trust())
        self.summary = self.reg.recipes_for("activity_summary")[0]
        self.presence = self.reg.recipes_for("presence")[0]

    def test_two_hop_nested_provenance_tier_and_union(self):
        pl = Planner(self.reg, discover=lambda n: {"compute": [_compute_prov("nC")]}.get(n, []))
        res = pl.need("activity_summary")
        p = res.plan
        self.assertEqual(p.depth, 2)

        # chain-max tier: activity_summary declares open, presence is sensitive → max.
        self.assertEqual(self.summary.manifest.get("consent_tier"), "open")
        self.assertEqual(p.effective_tier, "sensitive")
        self.assertEqual(p.manifest["consent_tier"], "sensitive")

        # cannot_detect = UNION, asserted field-by-field.
        for c in self.summary.cannot_detect:
            self.assertIn(c, p.cannot_detect)
        for c in self.presence.cannot_detect:
            self.assertIn(c, p.cannot_detect)
        self.assertEqual(len(p.cannot_detect),
                         len(set(self.summary.cannot_detect) | set(self.presence.cannot_detect)))
        # union also rides in the published manifest.
        self.assertEqual(p.manifest["cannot_detect"], p.cannot_detect)

        # fidelity concatenated hop-by-hop.
        self.assertIn(self.summary.fidelity, p.fidelity)
        self.assertIn(self.presence.fidelity, p.fidelity)

        # nested provenance — full lineage readable from the top.
        outer_in = p.provenance.inputs[0]
        self.assertEqual(outer_in["capability"], "presence")
        inner = outer_in["derived_from"]
        self.assertIsInstance(inner, Provenance)
        self.assertEqual(inner.recipe, "presence_from_activity")
        self.assertEqual(inner.inputs[0]["capability"], "compute")
        self.assertEqual(inner.inputs[0]["node_id"], "nC")
        # lineage renders three levels
        self.assertEqual(len(p.provenance.lineage_lines()), 4)


# ── GUARDS: cycle + depth (planner-only, temp registries) ──────────────────────

_PASSTHROUGH = '''\
def init(ctx):
    ctx["v"] = None
def on_frame(input_name, frame, ctx):
    fl = frame.get("fields", {})
    if "v" in fl:
        ctx["v"] = fl["v"]
    return reading(ctx)
def reading(ctx):
    if ctx.get("v") is None:
        return None
    return {"v": ctx["v"]}
'''


class TestGuards(unittest.TestCase):
    def setUp(self):
        self._home = TemporaryDirectory()
        self.dir = Path(self._home.name) / "recipes"
        self.dir.mkdir(parents=True)
        self.priv, self.pub = crypto.generate_keypair()
        self.trust = TrustStore(path=Path(self._home.name) / "trust.json")
        self.trust.add(self.pub, "test")

    def tearDown(self):
        self._home.cleanup()

    def _passthrough(self, name, provides_name, requires_hint, tier="open"):
        d = self.dir / name
        d.mkdir()
        recipe = {
            "name": name, "version": "1.0.0", "cost_rank_hint": 5, "unit_adaptations": {},
            "requires": [{"capability_hint": requires_hint,
                          "fields": {"v": {"type": "number"}}}],
            "provides": {"name": provides_name, "derived": True, "recipe": name,
                         "description": "passthrough fixture", "fidelity": "identity",
                         "cannot_detect": ["nothing"], "consent_tier": tier,
                         "reading": {"v": {"type": "number"}}, "streaming": False},
        }
        (d / "recipe.json").write_text(json.dumps(sign_recipe(recipe, self.priv)))
        (d / "transform.py").write_text(_PASSTHROUGH)
        (d / "test_frames.json").write_text(
            json.dumps([{"input": requires_hint, "fields": {"v": 1.0}, "ts": 1, "seq": 1}]))

    def _planner(self, providers=None):
        reg = Registry(recipes_dir=self.dir, trust=self.trust)
        providers = providers or {}
        return Planner(reg, discover=lambda n: providers.get(n, []))

    def test_self_feed_cycle_rejected(self):
        # recipe provides X and requires X → transitive self-requirement.
        self._passthrough("selfX", provides_name="X", requires_hint="X")
        res = self._planner().need("X")
        self.assertEqual(res.outcome, "refused")
        self.assertEqual(res.code, errors.DERIVATION_CYCLE)

    def test_mutual_cycle_rejected(self):
        # X requires Y, Y requires X.
        self._passthrough("recX", provides_name="X", requires_hint="Y")
        self._passthrough("recY", provides_name="Y", requires_hint="X")
        res = self._planner().need("X")
        self.assertEqual(res.outcome, "refused")
        self.assertEqual(res.code, errors.DERIVATION_CYCLE)

    def test_depth_three_rejected(self):
        # A ← B ← C ← leaf(real). C is the 3rd derivation → over the rail.
        self._passthrough("recA", provides_name="A", requires_hint="B")
        self._passthrough("recB", provides_name="B", requires_hint="C")
        self._passthrough("recC", provides_name="C", requires_hint="leaf")
        leaf = _prov("nL", "leaf", {"description": "leaf", "consent_tier": "open",
                                    "streaming": False, "reading": {"v": {"type": "number"}}})
        res = self._planner({"leaf": [leaf]}).need("A")
        self.assertEqual(res.outcome, "refused")
        self.assertEqual(res.code, errors.DEPTH_EXCEEDED)

    def test_depth_two_from_same_fixtures_allowed(self):
        # B ← C ← leaf is exactly two hops → allowed (proves the rail is at 3, not 2).
        self._passthrough("recB", provides_name="B", requires_hint="C")
        self._passthrough("recC", provides_name="C", requires_hint="leaf")
        leaf = _prov("nL", "leaf", {"description": "leaf", "consent_tier": "open",
                                    "streaming": False, "reading": {"v": {"type": "number"}}})
        res = self._planner({"leaf": [leaf]}).need("B")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.depth, MAX_DERIVATION_DEPTH)


# ── LIVE chain harness (both transports) ──────────────────────────────────────

class ChainMixin:
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

    def make_device(self, name, caps=("compute", "sensing"), approval=None):
        d = DeviceRuntime(name=name, capability_override=list(caps), lease_ttl=TTL,
                          approval_callback=approval)
        self._attach(d)
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

    def wait_until(self, pred, timeout=12.0, interval=0.1):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(interval)
        return pred()


class ChainLAN(ChainMixin):
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


class ChainDHT(ChainMixin):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="chain-bootstrap", udp_port=free_udp_port(), ttl=30)
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


# ── E2E: fully-local + across-the-wire (both transports) ──────────────────────

class ChainE2ETests(ChainMixin):

    def test_fully_local_two_hop_live(self):
        # one device provides real compute; the agent chains it locally:
        # compute → presence → activity_summary. No presence provider on the wire.
        dev = self.make_device("dev", caps=("compute",))
        ag = self.make_agent("ag")
        res = self.planner(ag, dev).need("activity_summary")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.depth, 2)
        dc = self.derive(res.plan, ag)
        self.assertTrue(self.wait_until(
            lambda: dc.reading() is not None and dc.reading().get("samples", 0) >= 2))
        self.assertEqual(dc.state, "active")
        # health nests the inner presence derivation.
        self.assertIn("inner", dc.health()["per_input"]["presence"])

    def test_across_the_wire_two_hop_live(self):
        # runtime A derives presence and PUBLISHES it; agent B derives
        # activity_summary from the published presence (single-hop from B's view).
        A_dev = self.make_device("A", caps=("compute",), approval=lambda r, a: True)
        A_ag = self.make_agent("A-ag")
        inner = self.derive(self.planner(A_ag, A_dev).need("presence").plan, A_ag)
        self.assertTrue(self.wait_until(lambda: inner.reading() is not None))
        inner.publish(A_dev)

        B = self.make_agent("B-ag")
        plB = self.planner(B, A_dev)
        res = plB.need("activity_summary")
        self.assertEqual(res.outcome, "derived")
        # from B's view this is single-hop onto a published-derived provider, whose
        # manifest still carries the chain-max sensitive tier.
        self.assertEqual(res.plan.effective_tier, "sensitive")
        self.assertTrue(res.plan.inputs[0]["provider"]["manifest"].get("derived"))
        outer = self.derive(res.plan, B)
        self.assertTrue(self.wait_until(
            lambda: outer.reading() is not None and outer.reading().get("samples", 0) >= 2,
            timeout=14.0))


class TestChainE2ELAN(ChainE2ETests, ChainLAN, unittest.TestCase):
    pass


class TestChainE2EDHT(ChainE2ETests, ChainDHT, unittest.TestCase):
    pass


# ── healing across hops (LAN) ─────────────────────────────────────────────────

class TestChainHealing(ChainLAN, unittest.TestCase):

    def test_inner_input_death_fails_required_outer(self):
        # provider P serves compute; agent A chains locally. Killing P kills the
        # inner presence's only input → presence fails → outer (required) fails.
        P = self.make_device("P", caps=("compute",))
        A = self.make_agent("A")
        res = self.planner(A, P).need("activity_summary")
        dc = self.derive(res.plan, A, heal_max_attempts=2, heal_shutdown_backoff_s=1.0)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        P.stop_swarm()                                   # inner input dies (device_shutdown)
        self.assertTrue(self.wait_until(lambda: dc.state == "failed", timeout=10.0),
                        "required inner death did not fail the outer")

    def test_inner_published_graceful_shutdown_propagates(self):
        # A publishes presence; B chains it. A departs gracefully → B's presence
        # input gets device_shutdown → B's outer (required presence) fails.
        A_dev = self.make_device("A", caps=("compute",), approval=lambda r, a: True)
        A_ag = self.make_agent("A-ag")
        inner = self.derive(self.planner(A_ag, A_dev).need("presence").plan, A_ag)
        self.assertTrue(self.wait_until(lambda: inner.reading() is not None))
        inner.publish(A_dev)

        B = self.make_agent("B-ag")
        res = self.planner(B, A_dev).need("activity_summary")
        outer = self.derive(res.plan, B, heal_max_attempts=2, heal_shutdown_backoff_s=1.0)
        self.assertTrue(self.wait_until(lambda: outer.reading() is not None, timeout=14.0))
        A_dev.stop_swarm()                               # publisher of presence departs
        self.assertTrue(self.wait_until(lambda: outer.state == "failed", timeout=10.0),
                        "inner publisher shutdown did not propagate to the outer")


class TestChainOptionalHealing(ChainLAN, unittest.TestCase):
    """Optional inner death degrades (does not fail) — uses a temp fixture recipe
    that marks its derived presence input optional."""

    _OPT_TRANSFORM = '''\
def init(ctx):
    ctx["seen"] = 0
def on_frame(input_name, frame, ctx):
    if "in_use" in frame.get("fields", {}):
        ctx["seen"] += 1
    return reading(ctx)
def reading(ctx):
    return {"seen": ctx["seen"]}
'''

    def setUp(self):
        super().setUp()
        # temp registry: copy presence_from_activity (so presence can be chained)
        # + an optional-presence outer recipe.
        self._home = TemporaryDirectory()
        rdir = Path(self._home.name) / "recipes"
        rdir.mkdir(parents=True)
        priv, pub = crypto.generate_keypair()
        trust = TrustStore(path=Path(self._home.name) / "trust.json")
        shutil.copytree(_REF / "presence_from_activity", rdir / "presence_from_activity")
        pr = json.loads((rdir / "presence_from_activity" / "recipe.json").read_text())
        (rdir / "presence_from_activity" / "recipe.json").write_text(json.dumps(sign_recipe(pr, priv)))
        opt = rdir / "opt_summary"; opt.mkdir()
        recipe = {
            "name": "opt_summary", "version": "1.0.0", "cost_rank_hint": 5,
            "unit_adaptations": {},
            "requires": [{"capability_hint": "presence", "optional": True,
                          "fields": {"in_use": {"type": "boolean"}}}],
            "provides": {"name": "opt_summary", "derived": True, "recipe": "opt_summary",
                         "description": "optional-presence fixture", "fidelity": "fixture",
                         "cannot_detect": ["x"], "consent_tier": "open",
                         "reading": {"seen": {"type": "number"}}, "streaming": False},
        }
        (opt / "recipe.json").write_text(json.dumps(sign_recipe(recipe, priv)))
        (opt / "transform.py").write_text(self._OPT_TRANSFORM)
        (opt / "test_frames.json").write_text(
            json.dumps([{"input": "presence", "fields": {"in_use": True}, "ts": 1, "seq": 1}]))
        trust.add(pub, "t")
        self.reg = Registry(recipes_dir=rdir, trust=trust)

    def tearDown(self):
        super().tearDown()
        self._home.cleanup()

    def test_optional_inner_death_degrades_outer(self):
        P = self.make_device("P", caps=("compute",))
        A = self.make_agent("A")
        res = self.planner(A, P).need("opt_summary")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.depth, 2)
        dc = self.derive(res.plan, A, heal_max_attempts=2, heal_shutdown_backoff_s=1.0)
        self.assertTrue(self.wait_until(lambda: dc.state == "active"))
        P.stop_swarm()                                   # optional inner's input dies
        self.assertTrue(self.wait_until(lambda: dc.state == "degraded", timeout=10.0),
                        "optional inner death should DEGRADE, not fail")
        self.assertNotEqual(dc.state, "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
