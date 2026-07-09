"""
tests/test_events.py — EVENT LAYER Phase 1: conditional event subscriptions.

Transport-agnostic (LANSwarm + DHTSwarm) like the lease tests: one mixin, two
concrete subclasses. The workhorse is a controllable VIRTUAL capability
("test_sensor") whose reading_fn returns a mutable box the test drives across
thresholds — so edge/re-arm behavior is deterministic (real hardware is not).

Covers:
  - fires on EDGE only (no repeat while level-high) + re-arm after false
  - baseline NEVER fires (even when the condition is already true at subscribe)
  - "changed" op fires on any value change
  - invalid conditions rejected (unknown field, op/type mismatch)
  - per-binding cap (event_cap_exceeded) vs per-capability ceiling
    (device_event_capacity) — distinct reasons
  - event carries the triggering snapshot + a gapless per-sub sequence
  - ALL event subs die on lease expiry (zero events after, multi-sweep window)
  - eval_hz clamp echoed; stream-hz clamp enforced (loop is not run at 1000 Hz)
  - VSO-reading condition end-to-end, including via DHT discovery (DHT subclass)

Run:  python3 -m unittest tests.test_events -v
"""

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from runtimes.device_runtime import DeviceRuntime, MAX_SAMPLE_HZ
from agents.remote_agent import RemoteAgent
from d2a.sense_layer import SenseLayer
from tests._env import use_tmp_home, restore_home


def setUpModule():
    use_tmp_home()


def tearDownModule():
    restore_home()


TTL  = 2      # short lease for the expiry test; sweeper interval = min(TTL/10,5)=0.2s
STEP = 0.35   # per-change settle; ~3 samples at 10 Hz


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _sensor_manifest() -> dict:
    return {
        "description": "test sensor",
        "reading": {"value": {"type": "number"}, "level": {"type": "string"}},
        "consent_tier": "open",
        "streaming": True,
    }


# ── transport-parametrized wire-level event tests ────────────────────────────────

class EventTestsMixin:
    """Shared event tests. Subclasses provide make_device/make_agent/_discover."""

    def setUp(self):
        self.devices = []
        self.agents = []
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

    def _settle(self):
        """No-op on LAN; DHT overrides to let a publish propagate."""
        pass

    # register a controllable virtual sensor on the device; box drives its reading
    def _add_sensor(self, device):
        box = {"value": 10.0, "level": "ok"}
        device._test_box = box
        device._register_virtual(
            "test_sensor", "sensor_file", "open", _sensor_manifest(),
            tags=["test_sensor", "open"], live_state=dict(box),
            reading_fn=lambda: dict(device._test_box), action_fn=lambda a, p: {},
        )
        self._settle()
        return box

    # register an async-action capability: long_task (cancellable, long_running),
    # fail_task (long_running, raises), read_all (single-pass, NOT long_running)
    def _add_async_dev(self, device):
        device._cancel_observed = []

        def action_fn(action, params, cancel=None):
            if action == "long_task":
                dur = float(params.get("dur", 1.0))
                deadline = time.time() + dur
                while time.time() < deadline:
                    if cancel is not None and cancel.is_set():
                        device._cancel_observed.append(True)
                        return {"cancelled": True}
                    time.sleep(0.03)
                return {"done": True, "dur": dur}
            if action == "fail_task":
                raise RuntimeError("boom")
            if action == "read_all":         # measured single-pass — stays sync
                return {"aggregate": {"count": 1, "mean": 42.0}, "members": 1}
            return {"error": "unknown"}

        man = {
            "description": "async dev",
            "reading": {"value": {"type": "number"}},
            "actions": {
                "long_task": {"description": "long", "long_running": True,
                              "params": {"dur": {"type": "number", "required": False}}},
                "fail_task": {"description": "fails", "long_running": True},
                "read_all":  {"description": "single-pass aggregate read"},
            },
            "consent_tier": "open", "streaming": True,
        }
        device._register_virtual(
            "async_dev", "sensor_file", "open", man,
            tags=["async_dev", "open"], live_state={"value": 1.0},
            reading_fn=lambda: {"value": 1.0}, action_fn=action_fn,
        )
        self._settle()

    def bind_async(self, agent, device):
        self._discover(agent, device, "async_dev")
        return agent.bind_remote_to(device.node_id, "async_dev")

    def bind_sensor(self, agent, device):
        self._discover(agent, device, "test_sensor")
        return agent.bind_remote_to(device.node_id, "test_sensor")

    def _collect(self, events):
        return lambda e: events.append(e)

    # 1 — fires on edge only + re-arm
    def test_edge_only_and_rearm(self):
        d = self.make_device("edgedev")
        box = self._add_sensor(d)
        a = self.make_agent("edgeag", auto_renew=False)
        r = self.bind_sensor(a, d)
        self.assertTrue(r.get("verified"))

        events = []
        resp = a.on_event(r, {"field": "value", "op": "gt", "value": 50},
                          self._collect(events), eval_hz=10)
        self.assertEqual(resp.get("status"), "subscribed")

        time.sleep(STEP)                 # baseline (10 < 50) — no fire
        box["value"] = 60; time.sleep(STEP)   # cross → fire 1
        box["value"] = 70; time.sleep(STEP)   # still high → NO fire
        box["value"] = 40; time.sleep(STEP)   # drop → re-arm, no event
        box["value"] = 80; time.sleep(STEP)   # cross again → fire 2

        self.assertEqual([e["seq"] for e in events], [1, 2],
                         "exactly two edges, gapless seq; no repeat while level-high")

    # 2 — baseline never fires, even when already true at subscribe
    def test_baseline_never_fires(self):
        d = self.make_device("basedev")
        box = self._add_sensor(d)
        a = self.make_agent("baseag", auto_renew=False)
        r = self.bind_sensor(a, d)

        box["value"] = 100                # already above threshold BEFORE subscribe
        events = []
        a.on_event(r, {"field": "value", "op": "gt", "value": 50},
                   self._collect(events), eval_hz=10)
        time.sleep(4 * STEP)              # many samples, condition true throughout
        self.assertEqual(events, [], "baseline (already-true) must not fire")

        box["value"] = 10; time.sleep(STEP)   # go false (re-arm)
        box["value"] = 90; time.sleep(STEP)   # now a real crossing → fire once
        self.assertEqual(len(events), 1)

    # 3 — "changed" op fires on any change
    def test_changed_op(self):
        d = self.make_device("chgdev")
        box = self._add_sensor(d)
        a = self.make_agent("chgag", auto_renew=False)
        r = self.bind_sensor(a, d)

        events = []
        resp = a.on_event(r, {"field": "level", "op": "changed"},
                          self._collect(events), eval_hz=10)
        self.assertEqual(resp.get("status"), "subscribed")

        time.sleep(STEP)                       # baseline "ok" — no fire
        box["level"] = "warn";   time.sleep(STEP)   # change → fire
        box["level"] = "warn";   time.sleep(STEP)   # same → no fire
        box["level"] = "danger"; time.sleep(STEP)   # change → fire
        self.assertEqual([e["seq"] for e in events], [1, 2])

    # 4 — invalid conditions rejected
    def test_invalid_conditions_rejected(self):
        d = self.make_device("baddev")
        self._add_sensor(d)
        a = self.make_agent("badag", auto_renew=False)
        r = self.bind_sensor(a, d)

        unknown = a.on_event(r, {"field": "nope", "op": "gt", "value": 1}, lambda e: None)
        self.assertEqual(unknown.get("error"), "invalid_condition")

        mism = a.on_event(r, {"field": "level", "op": "gt", "value": 1}, lambda e: None)
        self.assertEqual(mism.get("error"), "invalid_condition")

    # 5 & 6 — the two caps produce DISTINCT rejection reasons
    def test_per_binding_cap_reason(self):
        d = self.make_device("capdev")
        self._add_sensor(d)
        d._event_subs_per_binding = 2      # tiny per-binding cap
        d._event_cap_ceiling      = 99
        a = self.make_agent("capag", auto_renew=False)
        r = self.bind_sensor(a, d)

        self.assertEqual(a.on_event(r, {"field": "value", "op": "gt", "value": 10},
                                    lambda e: None, eval_hz=1).get("status"), "subscribed")
        self.assertEqual(a.on_event(r, {"field": "value", "op": "gt", "value": 20},
                                    lambda e: None, eval_hz=1).get("status"), "subscribed")
        over = a.on_event(r, {"field": "value", "op": "gt", "value": 30},
                          lambda e: None, eval_hz=1)
        self.assertEqual(over.get("error"), "event_cap_exceeded")

    def test_per_capability_ceiling_reason(self):
        d = self.make_device("ceildev")
        self._add_sensor(d)
        d._event_subs_per_binding = 8      # high per-binding
        d._event_cap_ceiling      = 2      # low device ceiling
        a = self.make_agent("ceilag", auto_renew=False)
        r = self.bind_sensor(a, d)

        a.on_event(r, {"field": "value", "op": "gt", "value": 10}, lambda e: None, eval_hz=1)
        a.on_event(r, {"field": "value", "op": "gt", "value": 20}, lambda e: None, eval_hz=1)
        over = a.on_event(r, {"field": "value", "op": "gt", "value": 30}, lambda e: None, eval_hz=1)
        self.assertEqual(over.get("error"), "device_event_capacity",
                         "device-wide ceiling is a DISTINCT reason from per-binding")

    # 7 — event carries the snapshot + gapless sequence
    def test_snapshot_and_gapless_sequence(self):
        d = self.make_device("snapdev")
        box = self._add_sensor(d)
        a = self.make_agent("snapag", auto_renew=False)
        r = self.bind_sensor(a, d)

        events = []
        a.on_event(r, {"field": "value", "op": "changed"}, self._collect(events), eval_hz=10)
        time.sleep(STEP)
        for v in (11.0, 12.0, 13.0, 14.0):
            box["value"] = v; time.sleep(STEP)

        self.assertGreaterEqual(len(events), 3)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)), "gapless per-sub sequence")
        for e in events:
            self.assertIn("reading", e)
            self.assertIn("raw", e["reading"])   # triggering snapshot present
            self.assertNoGap = e.get("_gap")     # normal delivery → no gap surfaced
            self.assertIsNone(e.get("_gap"))

    # 8 — ALL event subs die on lease expiry (multi-sweep window)
    def test_all_event_subs_die_on_expiry(self):
        d = self.make_device("expdev", lease_ttl=TTL)
        box = self._add_sensor(d)
        a = self.make_agent("expag", auto_renew=False)   # never renews → lapses
        r = self.bind_sensor(a, d)

        events = []
        a.on_event(r, {"field": "value", "op": "changed"}, self._collect(events), eval_hz=10)
        a.on_event(r, {"field": "level", "op": "changed"}, self._collect(events), eval_hz=10)
        time.sleep(STEP)
        box["value"] = 20; box["level"] = "warn"; time.sleep(STEP)
        self.assertGreater(len(events), 0, "events flow while the lease is live")

        time.sleep(TTL + 0.8)                       # lease lapses; sweeper tears down
        count_at_expiry = len(events)
        for i in range(4):                          # keep driving across several sweeps
            box["value"] = 100 + i; box["level"] = f"x{i}"
            time.sleep(0.3)
        self.assertEqual(len(events), count_at_expiry,
                         "zero events after lease expiry — every event sub died with it")

    # 9 — eval_hz clamp echoed; stream-hz clamp enforced
    def test_clamp_echoed_and_enforced(self):
        d = self.make_device("clampdev")
        self._add_sensor(d)
        a = self.make_agent("clampag", auto_renew=False)
        r = self.bind_sensor(a, d)

        # event eval_hz clamp — echoed in the subscribe response
        resp = a.on_event(r, {"field": "value", "op": "gt", "value": 999},
                          lambda e: None, eval_hz=1000)
        self.assertEqual(resp.get("effective_eval_hz"), MAX_SAMPLE_HZ)

        # stream hz clamp — ENFORCED: a 1000 Hz request must not run the loop at
        # 1000 Hz. Over ~0.6 s a clamped 10 Hz loop yields ~6 frames, never 100s.
        frames = []
        a.start_stream(r, lambda f: frames.append(1), hz=1000)
        time.sleep(0.6)
        a.stop_stream(r)
        self.assertLess(len(frames), 30,
                        f"loop clamped to {MAX_SAMPLE_HZ} Hz, got {len(frames)} frames in 0.6s")

    # 10 — explicit unsubscribe_event stops delivery
    def test_unsubscribe_event(self):
        d = self.make_device("unsubdev")
        box = self._add_sensor(d)
        a = self.make_agent("unsubag", auto_renew=False)
        r = self.bind_sensor(a, d)

        events = []
        resp = a.on_event(r, {"field": "value", "op": "changed"},
                          self._collect(events), eval_hz=10)
        esid = resp["event_sub_id"]
        time.sleep(STEP)
        box["value"] = 50; time.sleep(STEP)
        self.assertGreater(len(events), 0)

        a.off_event(r, esid)
        n = len(events)
        box["value"] = 60; time.sleep(2 * STEP)
        self.assertEqual(len(events), n, "no events after unsubscribe_event")

    # ── Phase 2: async tasks + reflex ──────────────────────────────────────────

    # 11 — long_running action returns a task_id IMMEDIATELY (5s-block bug fixed)
    def test_async_task_returns_immediately(self):
        d = self.make_device("asyncdev")
        self._add_async_dev(d)
        a = self.make_agent("asyncag", auto_renew=False)
        r = self.bind_async(a, d)

        # A 6 s action: synchronously this would block the handler PAST the 5 s
        # send_and_recv timeout — the exact old bug. Async, the call returns near
        # instantly with a task_id. (We don't wait it out — release cancels it.)
        t0 = time.time()
        resp = a.call_action(r, "long_task", {"dur": 6.0})
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0,
                        f"call returned in {elapsed:.2f}s (< the 5s timeout a sync 6s action would hit)")
        self.assertEqual(resp["result"]["status"], "running")
        self.assertTrue(resp["result"]["task_id"])
        a.release_binding(r)             # tears the lingering task down fast

    # 12 — completion arrives as a kind:"task" event with the result
    def test_async_completion_event(self):
        d = self.make_device("compdev")
        self._add_async_dev(d)
        a = self.make_agent("compag", auto_renew=False)
        r = self.bind_async(a, d)

        done = []
        resp = a.call_action(r, "long_task", {"dur": 0.5}, on_complete=lambda e: done.append(e))
        self.assertEqual(resp["result"]["status"], "running")
        time.sleep(1.2)
        self.assertEqual(len(done), 1, "completion event delivered")
        self.assertEqual(done[0]["kind"], "task")
        self.assertEqual(done[0]["status"], "done")
        self.assertEqual(done[0]["result"], {"done": True, "dur": 0.5})

    # 13 — task_status polls through running → done, and failed
    def test_task_status_polling(self):
        d = self.make_device("polldev")
        self._add_async_dev(d)
        a = self.make_agent("pollag", auto_renew=False)
        r = self.bind_async(a, d)

        resp = a.call_action(r, "long_task", {"dur": 0.8})
        tid = resp["result"]["task_id"]
        self.assertEqual(a.task_status(r, tid)["status"], "running")
        time.sleep(1.2)
        self.assertEqual(a.task_status(r, tid)["status"], "done")

        # failure state surfaces too
        fresp = a.call_action(r, "fail_task")
        ftid = fresp["result"]["task_id"]
        time.sleep(0.4)
        fstat = a.task_status(r, ftid)
        self.assertEqual(fstat["status"], "failed")
        self.assertIn("boom", fstat.get("error", ""))

    # 14 — task dies with lease: cancel token fires, completion suppressed, unknown
    def test_task_dies_with_lease(self):
        d = self.make_device("deathdev", lease_ttl=TTL)
        self._add_async_dev(d)
        a = self.make_agent("deathag", auto_renew=False)   # never renews → lapses
        r = self.bind_async(a, d)

        done = []
        resp = a.call_action(r, "long_task", {"dur": 30.0}, on_complete=lambda e: done.append(e))
        tid = resp["result"]["task_id"]
        self.assertEqual(a.task_status(r, tid)["status"], "running")

        time.sleep(TTL + 1.5)                       # lease lapses; sweeper cancels
        self.assertTrue(d._cancel_observed, "cooperative cancel token fired for the demo action")
        self.assertEqual(done, [], "completion event suppressed after lease death")
        self.assertEqual(a.task_status(r, tid)["status"], "unknown", "task record dropped")

    # 15 — read_all/verdict_all are NOT long_running (measured single-pass): sync
    def test_single_pass_action_stays_synchronous(self):
        d = self.make_device("syncdev")
        self._add_async_dev(d)
        a = self.make_agent("syncag", auto_renew=False)
        r = self.bind_async(a, d)

        resp = a.call_action(r, "read_all")
        self.assertEqual(resp["type"], "action_result")
        self.assertIn("aggregate", resp["result"])
        self.assertNotIn("task_id", resp["result"], "single-pass action returns inline, no task")


# ── LAN concrete ─────────────────────────────────────────────────────────────────

class TestEventsLAN(EventTestsMixin, unittest.TestCase):
    def _setup_transport(self):
        pass

    def _teardown_transport(self):
        pass

    def make_device(self, name, lease_ttl=60):
        d = DeviceRuntime(name=name, capability_override=["compute"], lease_ttl=lease_ttl)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name, auto_renew=True):
        a = RemoteAgent(name=name, auto_renew=auto_renew)
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


# ── DHT concrete (VSO condition end-to-end via DHT discovery) ─────────────────────

class TestEventsDHT(EventTestsMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="event-bootstrap", udp_port=free_udp_port(), ttl=30)
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

    def make_device(self, name, lease_ttl=60):
        d = DeviceRuntime(name=name, capability_override=["compute"], lease_ttl=lease_ttl)
        self._attach_dht(d)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)
        return d

    def make_agent(self, name, auto_renew=True):
        a = RemoteAgent(name=name, auto_renew=auto_renew)
        self._attach_dht(a)
        a.start()
        self.agents.append(a)
        time.sleep(0.3)
        return a

    # virtual cap is published to the DHT on _register_virtual; let it propagate
    def _settle(self):
        time.sleep(0.4)

    def _discover(self, agent, device, cap):
        agent.find_capability(cap)


# ── sense-layer verdict-transition emitter (Part 2 TODO closed) ──────────────────

class TestSenseVerdictEmitter(unittest.TestCase):
    """The event_emitter hook fires on a verdict TRANSITION and NEVER on the
    baseline — the same changed-op edge semantics as the wire event layer."""

    class _StubVerdict:
        def __init__(self, seq): self._seq = list(seq); self._i = 0
        def judge(self, normalized, features):
            v = self._seq[min(self._i, len(self._seq) - 1)]; self._i += 1
            return v, "advice"

    def _sense(self, verdict_seq):
        from d2a.sense_types import SenseRequest
        sl = SenseLayer({"compute": []}, "laptop")
        # stub the pipeline so verdicts are deterministic
        sl._intent_matcher.resolve = lambda req: ["src"]
        sl._raw_collector.collect  = lambda s: {"x": 1}
        sl._normalizer.normalize   = lambda raw: {"x": 1.0}
        sl._feature_extractor.extract = lambda n: {"names": [], "vector": []}
        sl._confidence_engine.score = lambda raw, n: 1.0
        sl._verdict_engine = self._StubVerdict(verdict_seq)
        return sl, SenseRequest

    def test_fires_only_on_transition(self):
        sl, SenseRequest = self._sense(["comfort", "comfort", "caution", "caution", "distress"])
        emitted = []
        sl.set_event_emitter(lambda etype, p: emitted.append((etype, p["from"], p["to"])))
        for _ in range(5):
            sl.handle(SenseRequest(resource="compute", shape="verdict"))
        # comfort(baseline,no fire) comfort(same) caution(fire) caution(same) distress(fire)
        self.assertEqual(emitted, [
            ("verdict_change", "comfort", "caution"),
            ("verdict_change", "caution", "distress"),
        ])

    def test_baseline_never_fires(self):
        sl, SenseRequest = self._sense(["distress"])
        emitted = []
        sl.set_event_emitter(lambda etype, p: emitted.append(etype))
        sl.handle(SenseRequest(resource="compute", shape="verdict"))
        self.assertEqual(emitted, [], "first verdict for a resource must not fire")


class TestReflexPath(unittest.TestCase):
    """Device-LOCAL reflex: condition → local action, NO agent, NO network."""

    def test_reflex_fires_locally_no_agent(self):
        from d2a.sense_types import SenseRequest
        # A device, but NO agent is ever created or bound.
        d = DeviceRuntime(name="reflexdev", capability_override=["compute"], lease_ttl=60)

        class _Stub:
            def __init__(s, seq): s.seq = list(seq); s.i = 0
            def judge(s, n, f):
                v = s.seq[min(s.i, len(s.seq) - 1)]; s.i += 1
                return v, "advice"

        d.sense._intent_matcher.resolve   = lambda req: ["x"]
        d.sense._raw_collector.collect    = lambda s: {"x": 1}
        d.sense._normalizer.normalize     = lambda r: {"x": 1.0}
        d.sense._feature_extractor.extract = lambda n: {"names": [], "vector": []}
        d.sense._confidence_engine.score  = lambda r, n: 1.0
        d.sense._verdict_engine = _Stub(["comfort", "comfort", "distress",
                                         "distress", "comfort", "distress"])

        d.wire_reflex_demo("distress")
        for _ in range(6):
            d.sense.handle(SenseRequest(resource="compute", shape="verdict"))

        # edge-fires: enters distress at idx 2 and again at idx 5 (re-armed by the
        # comfort at idx 4); stays-distress at idx 3 does NOT re-fire.
        self.assertEqual(len(d.reflex_events), 2)
        self.assertTrue(all(e["verdict"] == "distress" for e in d.reflex_events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
