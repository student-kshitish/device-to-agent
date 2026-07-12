"""
tests/test_derive_live.py — Phase 2 of CASE 4: the LIVE derivation engine.

Phase 1 ended at a plan. Phase 2 runs it: bind the inputs under real leases, feed
the transform, expose a live reading, self-heal on lease loss, degrade on
staleness, and shut down leaving zero device-side residue. These tests drive the
whole path against a REAL DeviceRuntime + RemoteAgent.

The binding layer is transport-agnostic (it drives whatever swarm the agent
holds), so the core lifecycle tests run on BOTH LANSwarm and DHTSwarm via two
concrete subclasses of LiveMixin — exactly the pattern test_leases uses. The
finer state-machine tests (gap resync, required-vs-optional, staleness,
shutdown-vs-expired timing) are logic, not transport, so they run once on LAN.

Run:  python3 -m unittest tests.test_derive_live -v
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
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from d2a_derive import Registry, Planner, TrustStore, sign_recipe, DerivedCapability
from d2a_derive.demo_scaffolding import register_demo_odometry, OdometrySource
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent
from tests._env import use_tmp_home, restore_home

_REF = Path(__file__).resolve().parent.parent / "d2a_derive" / "reference_recipes"

# short lease so the sweeper reaps a killed lease fast (interval = min(TTL/10, 5))
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


# ── a two-input fixture recipe (NOT a reference recipe) ───────────────────────
# Required `sensing` input + OPTIONAL `demo_odometry` input, so we can prove the
# required-gone → failed vs optional-gone → degraded split.

_DUAL_TRANSFORM = '''\
def init(ctx):
    ctx["temp"] = None
    ctx["x"] = None

def on_frame(input_name, frame, ctx):
    fl = frame.get("fields", {})
    if "thermal.max_temp_c" in fl:
        ctx["temp"] = fl["thermal.max_temp_c"]
    if "pose.x_m" in fl:
        ctx["x"] = fl["pose.x_m"]
    return reading(ctx)

def reading(ctx):
    if ctx.get("temp") is None:
        return None
    return {"value": round(float(ctx["temp"]), 2)}
'''

_DUAL_FRAMES = [
    {"input": "sensing", "fields": {"thermal.max_temp_c": 40.0}, "ts": 1.0, "seq": 1},
    {"input": "sensing", "fields": {"thermal.max_temp_c": 41.0}, "ts": 2.0, "seq": 2},
]


def _dual_recipe_dict() -> dict:
    return {
        "name": "dual_probe_fixture",
        "version": "1.0.0",
        "author_pubkey": "",
        "sig": "",
        "cost_rank_hint": 3,
        "unit_adaptations": {},
        "requires": [
            {"capability_hint": "sensing",
             "fields": {"thermal.max_temp_c": {"type": "number", "unit": "C", "min_hz": 0.2}}},
            {"capability_hint": "demo_odometry", "optional": True,
             "fields": {"pose.x_m": {"type": "number", "unit": "m", "min_hz": 1.0}}},
        ],
        "provides": {
            "name": "dual_metric",
            "derived": True,
            "recipe": "dual_probe_fixture",
            "fidelity": "fixture: echoes the last thermal reading; the odometry input is optional",
            "cannot_detect": ["anything real"],
            "description": "Two-input test fixture (required sensing + optional odometry).",
            "consent_tier": "open",
            "reading": {"value": {"type": "number"}},
            "streaming": False,
        },
    }


def _write_dual_recipe(dir_path: Path, priv: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    signed = sign_recipe(_dual_recipe_dict(), priv)
    (dir_path / "recipe.json").write_text(json.dumps(signed))
    (dir_path / "transform.py").write_text(_DUAL_TRANSFORM)
    (dir_path / "test_frames.json").write_text(json.dumps(_DUAL_FRAMES))


# ── shared live-derivation harness ────────────────────────────────────────────

class LiveMixin:
    """Subclasses provide _setup_transport / _teardown_transport / _attach and
    _seed. Everything else — recipes, planner, DerivedCapability — is shared."""

    def setUp(self):
        self.devices = []
        self.agents = []
        self.dcs = []
        self._home = TemporaryDirectory()
        self.recipes_dir = Path(self._home.name) / "recipes"
        self.recipes_dir.mkdir(parents=True)
        self.priv, self.pub = crypto.generate_keypair()
        self.trust = TrustStore(path=Path(self._home.name) / "trusted_authors.json")
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
        self._home.cleanup()
        time.sleep(0.05)

    # recipe install ----------------------------------------------------------

    def install_reference(self, name):
        dst = self.recipes_dir / name
        shutil.copytree(_REF / name, dst)
        recipe = json.loads((dst / "recipe.json").read_text())
        (dst / "recipe.json").write_text(json.dumps(sign_recipe(recipe, self.priv)))
        self.trust.add(self.pub, "test")
        return dst

    def install_dual(self):
        _write_dual_recipe(self.recipes_dir / "dual_probe_fixture", self.priv)
        self.trust.add(self.pub, "test")

    def registry(self):
        return Registry(recipes_dir=self.recipes_dir, trust=self.trust)

    # device / agent ----------------------------------------------------------

    def make_device(self, name, caps=("compute", "sensing"), odo_unit=None):
        d = DeviceRuntime(name=name, capability_override=list(caps), lease_ttl=TTL)
        self._attach(d)
        if odo_unit is not None:
            register_demo_odometry(d, unit=odo_unit,
                                   source=OdometrySource(unit=odo_unit, step_m=0.6, turn=0.7))
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
        reg = self.registry()

        def discover(name):
            with agent.swarm._lock:
                return [dict(r) for (nid, nm), r in agent.swarm.records.items()
                        if nm == name and isinstance(r.get("manifest"), dict)]
        return Planner(reg, discover=discover)

    def run_derived(self, plan, agent, **kw):
        dc = DerivedCapability(plan, agent, **kw)
        self.dcs.append(dc)
        return dc.start()

    def kill_lease(self, device, binding_id):
        """Force a binding's device-clock lease into the past; the sweeper reaps it
        and pushes lease_expired (a silent-vanish class loss). Device stays alive."""
        b = device.broker.get_binding(binding_id)
        tok = b.token
        b.token = tok.__class__(**{**tok.__dict__, "expires_at": time.time() - 1})

    def wait_until(self, pred, timeout=8.0, interval=0.1):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(interval)
        return pred()


# ── LAN concrete ──────────────────────────────────────────────────────────────

class LiveLAN(LiveMixin):
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


# ── DHT concrete ──────────────────────────────────────────────────────────────

class LiveDHT(LiveMixin):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="derive-bootstrap", udp_port=free_udp_port(), ttl=30)
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


# ── the transport-parametrized lifecycle tests ────────────────────────────────

class LiveDeriveTests(LiveMixin):
    """Mixed into LAN + DHT concretes below."""

    # 1 — lease-loss self-heal: kill mid-derivation, rebind, next reading flows.
    def test_lease_loss_self_heal_recovers(self):
        self.install_reference("trajectory_free_space_map")
        dev = self.make_device("dev", odo_unit="m")
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev)
        res = pl.need("free_space_map")
        self.assertEqual(res.outcome, "derived")

        dc = self.run_derived(res.plan, ag, heal_backoff_s=0.2, heal_max_attempts=5)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        before = dc.reading()["free_cells"]
        feed = dc._feeds[0]
        bid = feed.binding["binding_id"]

        # kill the odometry lease; sweeper pushes lease_expired → healer rebinds.
        self.kill_lease(dev, bid)
        self.assertTrue(self.wait_until(lambda: feed.rebind_count >= 1, timeout=10.0),
                        "healer did not rebind after lease loss")
        # recovery proof: state back to active AND fresh frames flow (map grows).
        self.assertTrue(self.wait_until(
            lambda: dc.state == "active" and dc.reading()["free_cells"] > before,
            timeout=10.0), "derived reading did not resume after self-heal")

    # 7 — unit adaptation applied numerically (cm provider → m in the transform).
    def test_unit_adaptation_applied_numerically(self):
        self.install_reference("trajectory_free_space_map")
        # provider emits centimetres; recipe declares cm→m; transform must see metres.
        dev = self.make_device("dev", odo_unit="cm")
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev)
        res = pl.need("free_space_map")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.inputs[0]["provider"]["manifest"]["reading"]["pose.x_m"]["unit"], "cm")

        dc = self.run_derived(res.plan, ag)
        # the scale factor for cm→m is 0.01 — assert the executor computed it.
        self.assertAlmostEqual(dc._feeds[0].scales["pose.x_m"], 0.01)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None, timeout=8.0))
        # grid cells are ~0.5 m; a cm→m bug (100× too big) would make coverage explode
        # into thousands of m². With correct scaling the spiral stays metre-scale.
        self.assertTrue(self.wait_until(
            lambda: dc.reading()["free_cells"] >= 2, timeout=8.0))
        cov = dc.reading()["coverage_m2"]
        self.assertLess(cov, 100.0, "cm not scaled to m — coverage exploded")

    # 6 — clean shutdown: close() releases every binding; zero device-side residue.
    def test_clean_shutdown_zero_residue(self):
        self.install_reference("trajectory_free_space_map")
        dev = self.make_device("dev", odo_unit="m")
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev)
        dc = self.run_derived(pl.need("free_space_map").plan, ag)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        # a stream sub is live on the device while running
        self.assertTrue(self.wait_until(lambda: len(dev._binding_subs) >= 1))

        dc.close()
        self.assertTrue(self.wait_until(lambda: not any(
            dev.broker.active_binds.values()), timeout=5.0),
            "device still holds an active bind after close")
        self.assertEqual(dev._binding_subs, {}, "device still has a stream sub after close")
        self.assertEqual(dc.state, "closed")


class TestLiveDeriveLAN(LiveDeriveTests, LiveLAN, unittest.TestCase):
    pass


class TestLiveDeriveDHT(LiveDeriveTests, LiveDHT, unittest.TestCase):
    pass


# ── LAN-only state-machine tests (logic, not transport) ───────────────────────

class TestHealBranches(LiveLAN, unittest.TestCase):

    # 2 — device_shutdown vs lease_expired branch differently: no immediate retry
    # on an announced departure.
    def test_shutdown_vs_expired_branch_differently(self):
        # LEASE_EXPIRED: device alive, healer rebinds PROMPTLY.
        self.install_reference("trajectory_free_space_map")
        dev = self.make_device("dev", odo_unit="m")
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev)
        dc = self.run_derived(pl.need("free_space_map").plan, ag,
                              heal_backoff_s=0.2, heal_shutdown_backoff_s=1.5,
                              heal_max_attempts=5)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        feed = dc._feeds[0]

        # time the first RECOVERY ATTEMPT (rediscovery) relative to the loss — that
        # is what the backoff gates, independent of the transport's discover cost.
        attempts = []
        orig_disc = ag.find_capability
        ag.find_capability = lambda name=None: (attempts.append(time.time()), orig_disc(name))[1]

        loss_t = time.time()
        self.kill_lease(dev, feed.binding["binding_id"])
        self.assertTrue(self.wait_until(lambda: feed.rebind_count >= 1, timeout=8.0))
        first_expired_attempt = min(t for t in attempts if t >= loss_t) - loss_t
        self.assertLess(first_expired_attempt, 1.5,
                        "lease_expired should retry promptly (before shutdown backoff)")

    def test_device_shutdown_no_immediate_retry_and_failed(self):
        # DEVICE_SHUTDOWN: announced departure → input gone immediately (required →
        # failed), and NO rebind attempt within the shutdown backoff window.
        self.install_reference("trajectory_free_space_map")
        dev = self.make_device("dev", odo_unit="m")
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev)
        dc = self.run_derived(pl.need("free_space_map").plan, ag,
                              heal_backoff_s=0.2, heal_shutdown_backoff_s=2.0,
                              heal_max_attempts=3)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        feed = dc._feeds[0]

        attempts = []
        orig_disc = ag.find_capability
        ag.find_capability = lambda name=None: (attempts.append(time.time()), orig_disc(name))[1]

        loss_t = time.time()
        dev.stop_swarm()                       # graceful departure → device_shutdown push
        # required odometry input goes gone → capability FAILED, promptly.
        self.assertTrue(self.wait_until(lambda: dc.state == "failed", timeout=6.0),
                        "required input shutdown did not fail the capability")
        self.assertEqual(feed.state, "gone")
        # and no recovery attempt (rediscovery) happened in the first second — the
        # shutdown backoff (2.0s) deliberately holds off, unlike lease_expired.
        early = [t for t in attempts if t - loss_t < 1.0]
        self.assertEqual(early, [], "device_shutdown must NOT retry immediately")


class TestRequiredVsOptional(LiveLAN, unittest.TestCase):

    def _setup(self):
        self.install_dual()
        # sensing on one device, odometry on another → shut one down independently.
        self.dev_s = self.make_device("dev-sensing", caps=("compute", "sensing"))
        self.dev_o = self.make_device("dev-odo", caps=("compute",), odo_unit="m")
        self.ag = self.make_agent("ag")
        pl = self.planner(self.ag, self.dev_s, self.dev_o)
        res = pl.need("dual_metric")
        self.assertEqual(res.outcome, "derived")
        # sanity: two inputs, odometry marked optional
        opt = {f.hint: f.optional for f in DerivedCapability(res.plan, self.ag)._feeds}
        self.assertEqual(opt, {"sensing": False, "demo_odometry": True})
        return res.plan

    def test_required_input_gone_fails(self):
        plan = self._setup()
        dc = self.run_derived(plan, self.ag, heal_max_attempts=2, heal_shutdown_backoff_s=1.0)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        self.dev_s.stop_swarm()                # kill the REQUIRED sensing input
        self.assertTrue(self.wait_until(lambda: dc.state == "failed", timeout=6.0),
                        "required input gone must fail the capability")

    def test_optional_input_gone_degrades(self):
        plan = self._setup()
        dc = self.run_derived(plan, self.ag, heal_max_attempts=2, heal_shutdown_backoff_s=1.0)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        self.dev_o.stop_swarm()                # kill the OPTIONAL odometry input
        self.assertTrue(self.wait_until(lambda: dc.state == "degraded", timeout=6.0),
                        "optional input gone must DEGRADE, not fail")
        self.assertNotEqual(dc.state, "failed")


class TestStaleness(LiveLAN, unittest.TestCase):

    def test_staleness_degrades_then_recovers(self):
        self.install_reference("thermal_ambient_proxy")
        dev = self.make_device("dev", caps=("compute", "sensing"))
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev)
        res = pl.need("ambient_temp")
        self.assertEqual(res.outcome, "derived")

        # force a PULL feed (streaming=False) so we can starve it deterministically
        # by making request_data stop returning readings, then restore.
        prov = res.plan.inputs[0]["provider"]
        prov["manifest"] = {**prov["manifest"], "streaming": False}

        dc = self.run_derived(res.plan, ag, staleness_factor=3.0, monitor_interval_s=0.1)
        feed = dc._feeds[0]
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        self.assertEqual(dc.state, "active")

        # starve the feed: request_data returns a non-reading (transient device
        # trouble that is NOT a lease loss) → no frames ingested → staleness fires.
        orig = ag.request_data
        gate = {"starve": True}
        def gated(binding, capability=None):
            if gate["starve"]:
                return {"type": "error", "code": "temporarily_unavailable"}
            return orig(binding, capability)
        ag.request_data = gated

        self.assertTrue(self.wait_until(lambda: feed.state == "degraded", timeout=6.0),
                        "input did not go degraded on staleness")
        self.assertEqual(dc.state, "degraded")

        # recovery: frames resume → input flips back to active.
        gate["starve"] = False
        self.assertTrue(self.wait_until(
            lambda: feed.state == "active" and dc.state == "active", timeout=6.0),
            "did not recover to active after frames resumed")


class TestGapResync(LiveLAN, unittest.TestCase):

    def test_gap_increments_count_and_triggers_one_reread(self):
        self.install_reference("trajectory_free_space_map")
        dev = self.make_device("dev", odo_unit="m")
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev)
        res = pl.need("free_space_map")

        # build but DON'T start() — drive _ingest by hand so the seq gap is exact
        # and no background feed races the assertion.
        dc = DerivedCapability(res.plan, ag)
        self.dcs.append(dc)
        dc.module.init(dc._ctx)
        feed = dc._feeds[0]
        self.assertTrue(dc._bind_feed(feed), "manual bind for gap test failed")

        reread = {"n": 0}
        orig = ag.request_data
        def counting(binding, capability=None):
            reread["n"] += 1
            return orig(binding, capability)
        ag.request_data = counting

        # consecutive seq — no gap, no resync
        dc._ingest(feed, {"raw": {"pose": {"x_m": 0.0, "y_m": 0.0}}, "seq": 1, "ts": 1.0})
        self.assertEqual(feed.gap_count, 0)
        self.assertEqual(reread["n"], 0)

        # jump seq 1 → 5 (three frames missed): exactly one gap, exactly one re-read
        dc._ingest(feed, {"raw": {"pose": {"x_m": 0.6, "y_m": 0.1}}, "seq": 5, "ts": 2.0})
        self.assertEqual(feed.gap_count, 1)
        self.assertEqual(reread["n"], 1, "a detected gap must trigger exactly one resync")


if __name__ == "__main__":
    unittest.main(verbosity=2)
