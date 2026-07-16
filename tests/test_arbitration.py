"""
tests/test_arbitration.py — PHASE 12: multi-agent arbitration (v1.12).

A D2A-original concept (neither MCP nor A2A has it — software tools are copyable,
physical devices are singular): a contending agent may attach a CONTENTION CLAIM
to its bind_request; the device arbitrates by an OWNER-declared policy; the loser
is preempted GRACEFULLY (notice + re-queue), never silently cut.

Covers (the full Phase 12 list):
  - claim vocabulary: fixed priority set validated, malformed claims rejected
  - owner policy governs: an agent-claimed priority alone does NOT preempt —
    without owner opt-in a "safety" claim only orders the queue
  - safety preempts once the owner allows it; the victim gets a graceful
    `preempted` push (reason + winning level + re-queue position) and its lease
    is marked lost with the distinct preempted_by_arbitration code
  - remote raw-int priority can no longer preempt (the pre-v1.12 hole, closed)
  - anti-gaming: the per-agent claim-rate limit refuses (claim_rate_limited)
    and the refusal is audited
  - arbitration decisions audited (who was preempted for whom, under which
    policy, on whose stated claim) in the signed hash-chained log
  - EXTENDS broker preemption (may_preempt gate on the ONE path — local/direct
    callers unchanged; the pure-broker preemption invariants test in
    test_leases.py runs untouched)
  - waitqueue re-queue mechanics: a freed slot's grant is PUSHED (signed
    waitqueue_granted) and adopted by the queued agent
  - claims order the waitqueue by level even without preemption rights
  - both transports (LAN + DHT)

Run:  python3 -m unittest tests.test_arbitration -v
"""

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a import crypto, errors
from d2a import arbitration as _arbitration
from d2a.broker import CapabilityBroker
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
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


# ── 1. claim vocabulary ───────────────────────────────────────────────────────────

class TestClaimVocabulary(unittest.TestCase):
    def test_valid_claim_normalizes(self):
        c = _arbitration.validate_claim(
            {"priority": "urgent", "intent": "thermal emergency", "max_wait": 30})
        self.assertEqual(c["priority"], "urgent")
        self.assertEqual(c["max_wait"], 30.0)

    def test_fixed_priority_set(self):
        self.assertEqual(set(_arbitration.CLAIM_LEVELS),
                         {"routine", "elevated", "urgent", "safety"})
        # ordering: safety strongest (lowest number), routine == legacy default 5
        self.assertLess(_arbitration.CLAIM_LEVELS["safety"],
                        _arbitration.CLAIM_LEVELS["urgent"])
        self.assertEqual(_arbitration.CLAIM_LEVELS["routine"], 5)

    def test_malformed_claims_rejected(self):
        for bad in ({"priority": "ultra"},                       # not in the set
                    {"priority": "urgent", "extra": 1},          # unknown key
                    "urgent",                                    # not an object
                    {"priority": "urgent", "max_wait": -1},      # bad wait
                    {"priority": "urgent", "intent": "x" * 201}):  # oversized intent
            with self.assertRaises(_arbitration.ClaimError):
                _arbitration.validate_claim(bad)

    def test_effective_priority_clamps_legacy_ints(self):
        # No claim: a remote raw int can lower, never raise, effective priority.
        self.assertEqual(_arbitration.effective_priority(None, 1), 5)
        self.assertEqual(_arbitration.effective_priority(None, 7), 7)
        # A claim maps to its level's number regardless of the legacy int.
        c = _arbitration.validate_claim({"priority": "safety"})
        self.assertEqual(_arbitration.effective_priority(c, 9), 1)


# ── 2. owner arbitration policy ─────────────────────────────────────────────────

class TestArbitrationPolicy(unittest.TestCase):
    def test_default_denies_all_preemption(self):
        p = _arbitration.ArbitrationPolicy()
        for level in _arbitration.CLAIM_LEVELS:
            self.assertFalse(p.may_preempt(level, "anything"),
                             f"{level} must not preempt without owner opt-in")

    def test_owner_opt_in_global_and_per_capability(self):
        p = _arbitration.ArbitrationPolicy()
        p.allow_preemption("safety")
        p.allow_preemption("urgent", capability="camera")
        self.assertTrue(p.may_preempt("safety", "anything"))
        self.assertTrue(p.may_preempt("urgent", "camera"))
        self.assertFalse(p.may_preempt("urgent", "compute"))
        p.revoke_preemption("safety")
        self.assertFalse(p.may_preempt("safety", "anything"))
        self.assertEqual(p.preempt_levels("camera"), ["urgent"])

    def test_unknown_level_rejected_loudly(self):
        with self.assertRaises(_arbitration.ClaimError):
            _arbitration.ArbitrationPolicy().allow_preemption("mega")

    def test_claim_rate_sliding_window(self):
        p = _arbitration.ArbitrationPolicy()
        p.set_claim_rate(max_claims=2, window=60.0)
        self.assertTrue(p.note_claim("a", "urgent", now=0.0))
        self.assertTrue(p.note_claim("a", "elevated", now=1.0))
        self.assertFalse(p.note_claim("a", "urgent", now=2.0), "over budget")
        self.assertTrue(p.note_claim("b", "urgent", now=2.0), "per-agent budget")
        self.assertTrue(p.note_claim("a", "routine", now=2.0),
                        "routine claims are never limited")
        self.assertTrue(p.note_claim("a", "urgent", now=61.5), "window slid")


# ── 3. broker: may_preempt EXTENDS the one preemption path ────────────────────────

class _FakeRuntime:
    private_key, public_key = crypto.generate_keypair()
    node_id = crypto.derive_node_id(public_key)
    lease_ttl = 60
    def get_capability(self, name):
        return True if name == "x" else None


class TestBrokerMayPreempt(unittest.TestCase):
    def _broker(self):
        b = CapabilityBroker(_FakeRuntime())
        b.quotas = {"x": 1}
        return b

    def test_default_true_preserves_local_preemption(self):
        b = self._broker()
        b.request_bind("A", "x", [], priority=5)
        r = b.request_bind("B", "x", [], priority=1)      # local/direct — trusted
        self.assertEqual(r["status"], "granted_by_preemption")
        # v1.12 additive result fields for the graceful notice
        self.assertEqual(r["preempted_agent_id"], "A")
        self.assertTrue(r["preempted_binding_id"])
        self.assertEqual(r["victim_queue_position"], 1)

    def test_may_preempt_false_queues_instead(self):
        b = self._broker()
        rA = b.request_bind("A", "x", [], priority=5)
        r = b.request_bind("B", "x", [], priority=1, may_preempt=False)
        self.assertEqual(r["status"], "queued", "numeric superiority alone must not evict")
        # holder untouched
        self.assertEqual(b.get_binding(rA["binding_id"]).status, "active")
        # but the queue is ORDERED by the number (B ahead of a later routine C)
        b.request_bind("C", "x", [], priority=5, may_preempt=False)
        self.assertEqual([e[1] for e in b.waitqueue["x"]], ["B", "C"])

    def test_max_wait_deadline_pruned_and_skipped_at_grant(self):
        b = self._broker()
        b.request_bind("A", "x", [], priority=5)
        past = {"deadline": time.time() - 1}
        b.request_bind("B", "x", [], priority=5, may_preempt=False, claim_meta=past)
        b.request_bind("C", "x", [], priority=5, may_preempt=False)
        # grant path skips the stale entry and grants the next one
        r = b.release_bind("A", "x")
        self.assertEqual(r["next_agent_id"], "C", "expired-max_wait entry skipped")
        # prune removes stale entries device-side
        b.request_bind("D", "x", [], priority=5, may_preempt=False, claim_meta=dict(past))
        pruned = b.prune_waitqueues()
        self.assertEqual([p["agent_id"] for p in pruned], ["D"])
        self.assertEqual(b.waitqueue["x"], [])


# ── 4. wire: owner-governed arbitration over both transports ─────────────────────

class ArbitrationWireMixin:
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

    def _settle(self):
        pass

    def _discover(self, agent, device, cap):
        raise NotImplementedError

    def _bind(self, agent, device, cap, priority=5, claim=None):
        self._discover(agent, device, cap)
        return agent.bind_remote_to(device.node_id, cap, priority, claim=claim)

    def _spy_pushes(self, agent, types=("preempted", "waitqueue_granted")):
        """Wrap the agent's inbound handler to capture raw pushes (still delegated)."""
        seen = []
        orig = agent.swarm.message_handler
        def spy(msg):
            if msg.get("type") in types:
                seen.append(msg)
            return orig(msg)
        agent.swarm.message_handler = spy
        return seen

    def _wait(self, cond, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    # owner policy governs: a claim alone does NOT preempt
    def test_claimed_priority_alone_does_not_preempt(self):
        d = self.make_device("nogift")
        a1 = self.make_agent("holder1")
        a2 = self.make_agent("claimer1")
        r1 = self._bind(a1, d, "compute")
        self.assertTrue(r1.get("verified"), r1)
        # B claims SAFETY — the strongest level — but the owner set no policy.
        r2 = self._bind(a2, d, "compute",
                        claim={"priority": "safety", "intent": "I want it"})
        self.assertEqual(r2.get("status"), "queued",
                         "claiming grants nothing — no owner policy, no eviction")
        self.assertEqual(r2.get("queue_position"), 1)
        # holder untouched and still serving
        frame = a1.request_data(r1)
        self.assertEqual(frame.get("capability"), "compute")

    # safety preempts under owner policy; the victim is notified GRACEFULLY
    def test_owner_sanctioned_preemption_graceful_and_audited(self):
        d = self.make_device("arb")
        d.arbitration.allow_preemption("safety")
        a1 = self.make_agent("victim")
        a2 = self.make_agent("winner")
        lost = []
        a1.on_lease_lost = lambda bid, reason: lost.append((bid, reason))
        pushes = self._spy_pushes(a1)

        r1 = self._bind(a1, d, "compute")
        self.assertTrue(r1.get("verified"), r1)
        r2 = self._bind(a2, d, "compute",
                        claim={"priority": "safety",
                               "intent": "thermal shutdown imminent"})
        self.assertEqual(r2.get("status"), "granted_by_preemption", r2)
        self.assertTrue(r2.get("verified"))

        # the victim got the graceful push: reason + winning level + requeue
        self.assertTrue(self._wait(lambda: pushes), "victim never got the notice")
        note = pushes[0]
        self.assertEqual(note["type"], "preempted")
        self.assertEqual(note["code"], errors.PREEMPTED_BY_ARBITRATION)
        self.assertEqual(note["binding_id"], r1["binding_id"])
        self.assertEqual(note["winning_priority"], "safety")
        self.assertTrue(note["requeued"])
        self.assertEqual(note["queue_position"], 1)
        # and its lease is marked lost with the DISTINCT code (not expiry/shutdown)
        self.assertTrue(self._wait(lambda: lost))
        self.assertEqual(lost[0], (r1["binding_id"], errors.PREEMPTED_BY_ARBITRATION))
        with self.assertRaises(LeaseLostError):
            a1.request_data(r1)
        # the winner is actually served
        self.assertEqual(a2.request_data(r2).get("capability"), "compute")

        # the decision is in the signed audit: who was preempted for whom,
        # under which policy, on whose STATED claim
        head = d._audit_log().head()
        self.assertEqual(head["kind"], "arbitration")
        self.assertEqual(head["result"], "preempted")
        self.assertEqual(head["claimant_agent_id"], a2.agent_id)
        self.assertEqual(head["victim_agent_id"], a1.agent_id)
        self.assertEqual(head["victim_binding_id"], r1["binding_id"])
        self.assertEqual(head["claim"]["priority"], "safety")
        self.assertEqual(head["claim"]["intent"], "thermal shutdown imminent")
        self.assertEqual(head["policy_allowed"], ["safety"])
        self.assertTrue(head["requeued"])
        ok, _ = d._audit_log().verify_chain()
        self.assertTrue(ok)

    # the pre-v1.12 hole: a remote raw int can no longer evict
    def test_remote_raw_int_priority_cannot_preempt(self):
        d = self.make_device("clamp")
        a1 = self.make_agent("holder2")
        a2 = self.make_agent("intgamer")
        r1 = self._bind(a1, d, "compute", priority=5)
        self.assertTrue(r1.get("verified"), r1)
        r2 = self._bind(a2, d, "compute", priority=1)      # the old eviction button
        self.assertEqual(r2.get("status"), "queued",
                         "a remote raw int must be clamped to the routine band")
        self.assertEqual(a1.request_data(r1).get("capability"), "compute")

    # malformed claim → clean coded denial
    def test_invalid_claim_denied(self):
        d = self.make_device("badclaim")
        a = self.make_agent("badclaimag")
        r = self._bind(a, d, "compute", claim={"priority": "ultra"})
        self.assertEqual(r.get("status"), "denied")
        self.assertEqual(r.get("code"), errors.INVALID_CLAIM)

    # anti-gaming: claim-rate limit refuses (not downgrades) and is audited
    def test_claim_rate_limit_refused_and_audited(self):
        d = self.make_device("spam")
        d.arbitration.set_claim_rate(max_claims=1, window=60.0)
        a = self.make_agent("spammer")
        r1 = self._bind(a, d, "compute",
                        claim={"priority": "elevated", "intent": "first"})
        self.assertTrue(r1.get("verified"), r1)            # within budget
        r2 = self._bind(a, d, "sensing",
                        claim={"priority": "urgent", "intent": "second"})
        self.assertEqual(r2.get("status"), "denied")
        self.assertEqual(r2.get("code"), errors.CLAIM_RATE_LIMITED)
        head = d._audit_log().head()
        self.assertEqual(head["kind"], "arbitration")
        self.assertEqual(head["result"], "claim_rate_limited")
        self.assertEqual(head["claimant_agent_id"], a.agent_id)
        self.assertEqual(head["claim"]["priority"], "urgent")

    # claims order the waitqueue by level even with NO preemption rights
    def test_claims_order_waitqueue_without_evicting(self):
        d = self.make_device("order")
        a1 = self.make_agent("holder3")
        a2 = self.make_agent("routineq")
        a3 = self.make_agent("urgentq")
        r1 = self._bind(a1, d, "compute")
        self.assertTrue(r1.get("verified"), r1)
        r2 = self._bind(a2, d, "compute")                  # no claim → routine band
        self.assertEqual(r2.get("status"), "queued")
        self.assertEqual(r2.get("queue_position"), 1)
        r3 = self._bind(a3, d, "compute",
                        claim={"priority": "urgent", "intent": "ahead please"})
        self.assertEqual(r3.get("status"), "queued", "no policy → no eviction")
        self.assertEqual(r3.get("queue_position"), 1, "but the level orders the queue")
        self.assertEqual([e[1] for e in d.broker.waitqueue["compute"]],
                         [a3.agent_id, a2.agent_id])

    # re-queue mechanics: a freed slot's grant is PUSHED, verified, adopted
    def test_waitqueue_grant_pushed_signed_and_adopted(self):
        d = self.make_device("regrant")
        a1 = self.make_agent("releaser")
        a2 = self.make_agent("waiter")
        regrants = []
        a2.on_regrant = lambda binding: regrants.append(binding)
        pushes = self._spy_pushes(a2)

        r1 = self._bind(a1, d, "compute")
        self.assertTrue(r1.get("verified"), r1)
        r2 = self._bind(a2, d, "compute")
        self.assertEqual(r2.get("status"), "queued")

        a1.release_binding(r1)                             # slot frees → auto-grant
        self.assertTrue(self._wait(lambda: regrants), "grant push never adopted")
        note = pushes[0]
        self.assertEqual(note["type"], "waitqueue_granted")
        self.assertEqual(note["agent_id"], a2.agent_id)
        binding = regrants[0]
        self.assertTrue(binding.get("verified"))
        self.assertEqual(binding.get("capability_name"), "compute")
        # the adopted binding actually serves data
        frame = a2.request_data(binding)
        self.assertEqual(frame.get("capability"), "compute")


# ── LAN concrete ─────────────────────────────────────────────────────────────────

class TestArbitrationLAN(ArbitrationWireMixin, unittest.TestCase):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass

    def make_device(self, name):
        d = DeviceRuntime(name=f"lan-arb-{name}",
                          capability_override=["compute", "sensing"], lease_ttl=120)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=f"lan-arb-{name}", auto_renew=False)
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
                agent.swarm.records[(device.node_id, c.name)] = rec
        agent.swarm.add_known_peer(device.node_id, ip, port)


# ── DHT concrete ─────────────────────────────────────────────────────────────────

class TestArbitrationDHT(ArbitrationWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="arb-bootstrap", udp_port=free_udp_port(), ttl=30)
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
        d = DeviceRuntime(name=f"dht-arb-{name}",
                          capability_override=["compute", "sensing"], lease_ttl=120)
        self._attach_dht(d)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=f"dht-arb-{name}", auto_renew=False)
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
