"""
tests/test_keyed_approval.py — PHASE 10A: remote KEYED owner approval.

Phase 8 approved an intervention plan with a LOCAL console callback and recorded
"device_owner@local" — a local attestation, not a cryptographic proof of WHO
approved. 10A adds an OWNER KEYPAIR (a principal distinct from the device host
key), TOFU-registered on the device, that can approve a plan by SIGNING its
plan_hash over the wire — and the audit then records the owner pubkey + signature.

Coverage:
  1. TestOwnerRegistration — set_owner_pubkey TOFU pin (foreign rejected, rotate),
     persistence across restart, and the node-descriptor owner_pubkey slot.
  2. TestKeyedApprovalVerify — _verify_owner_approval: valid; bound to plan_hash
     (a sig for plan X rejected for plan Y); replay rejected; stale ts; foreign
     key; unregistered device. Plus the resolver priority (keyed > local > pending
     > deny) with NO owner prompt side effect.
  3. KeyedApprovalWireMixin (+ LAN / DHT) — the two-round flow end to end
     (pending → owner signs → executes), the audit records owner pubkey + sig and
     survives restart with the chain intact, and an unregistered-owner device
     falls back to the local callback unchanged. Uses process_release (no systemd).

Persisted keys / pins / owner-pins / audit are isolated to a tmpdir (never ~/.d2a).
"""

import os
import socket
import subprocess
import tempfile
import time
import unittest

from d2a import crypto, signing, errors
from d2a import audit as _audit
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


_U = [0]


def _uniq(p: str) -> str:
    _U[0] += 1
    return f"{p}-{_U[0]}-{int(time.time()*1000) % 100000}"


def _release_plan(pid: int) -> dict:
    return {
        "action": "release", "params": {"pid": pid},
        "evidence": {"field": "holder_count", "reading": {"holder_count": 1}},
        "expected": "node released (0 holders)",
        "verify": {"diagnostic": "device_node_health",
                   "condition": {"field": "holder_count", "op": "eq", "value": 0}},
        "reversible": False, "reversible_how": "", "reversible_ack": True,
    }


# ── 1. owner registration (TOFU) ────────────────────────────────────────────────

class TestOwnerRegistration(unittest.TestCase):
    def test_tofu_pin_rotate_and_persist(self):
        name = _uniq("regdev")
        dev = DeviceRuntime(name=name, capability_override=["compute"])
        try:
            self.assertIsNone(dev.owner_pubkey)
            _, owner_pub = crypto.generate_keypair()
            r = dev.set_owner_pubkey(owner_pub)
            self.assertEqual(r["status"], "ok")
            self.assertEqual(dev.owner_pubkey, owner_pub)
            self.assertEqual(r["owner_fingerprint"], "owner:" + crypto.derive_node_id(owner_pub))
            # descriptor slot now populated
            self.assertEqual(dev._node_header(0, False).get("owner_pubkey"), owner_pub)
            # TOFU: a different key is rejected without rotate
            _, other_pub = crypto.generate_keypair()
            bad = dev.set_owner_pubkey(other_pub)
            self.assertEqual(bad["code"], errors.OWNER_KEY_MISMATCH)
            self.assertEqual(dev.owner_pubkey, owner_pub)          # unchanged
            # explicit rotate replaces it
            rot = dev.set_owner_pubkey(other_pub, rotate=True)
            self.assertEqual(rot["status"], "ok")
            self.assertTrue(rot["rotated"])
            self.assertEqual(dev.owner_pubkey, other_pub)
        finally:
            dev.stop_swarm()

        # persistence: a fresh runtime with the same name reads the pinned owner.
        dev2 = DeviceRuntime(name=name, capability_override=["compute"])
        try:
            self.assertEqual(dev2.owner_pubkey, other_pub)
        finally:
            dev2.stop_swarm()


# ── 2. keyed-approval verification logic (unit) ─────────────────────────────────

class TestKeyedApprovalVerify(unittest.TestCase):
    def setUp(self):
        self.dev = DeviceRuntime(name=_uniq("kvdev"), capability_override=["compute"])
        self.owner_priv, self.owner_pub = crypto.generate_keypair()
        self.dev.set_owner_pubkey(self.owner_pub)
        self.ph = "deadbeef" * 8

    def tearDown(self):
        try: self.dev.stop_swarm()
        except Exception: pass

    def _sign(self, plan_hash=None, **kw):
        return signing.sign_owner_approval(plan_hash or self.ph, self.dev.node_id,
                                           self.owner_priv, self.owner_pub, **kw)

    def test_valid_keyed_approval(self):
        d = self.dev._verify_owner_approval(self._sign(), self.ph)
        self.assertTrue(d["approved"])
        self.assertEqual(d["approver"], "owner:" + crypto.derive_node_id(self.owner_pub))
        self.assertEqual(d["owner_pubkey"], self.owner_pub)

    def test_bound_to_plan_hash(self):
        # a signature produced for plan X must not approve plan Y
        oa = self._sign(plan_hash=self.ph)
        d = self.dev._verify_owner_approval(oa, "ffff" * 16)
        self.assertFalse(d["approved"])
        self.assertEqual(d["code"], errors.OWNER_SIG_INVALID)

    def test_replay_rejected(self):
        oa = self._sign()
        self.assertTrue(self.dev._verify_owner_approval(oa, self.ph)["approved"])
        again = self.dev._verify_owner_approval(oa, self.ph)   # same nonce
        self.assertFalse(again["approved"])
        self.assertEqual(again["code"], errors.OWNER_APPROVAL_STALE)

    def test_stale_ts_rejected(self):
        oa = self._sign(ts=time.time() - (signing.REPLAY_WINDOW_SECONDS + 30))
        d = self.dev._verify_owner_approval(oa, self.ph)
        self.assertFalse(d["approved"])
        self.assertEqual(d["code"], errors.OWNER_APPROVAL_STALE)

    def test_foreign_owner_key_rejected(self):
        # a well-formed signature from a key that is NOT the pinned owner
        other_priv, other_pub = crypto.generate_keypair()
        oa = signing.sign_owner_approval(self.ph, self.dev.node_id, other_priv, other_pub)
        d = self.dev._verify_owner_approval(oa, self.ph)
        self.assertFalse(d["approved"])
        self.assertEqual(d["code"], errors.OWNER_KEY_MISMATCH)

    def test_unregistered_device_rejects_keyed(self):
        dev2 = DeviceRuntime(name=_uniq("noowner"), capability_override=["compute"])
        try:
            d = dev2._verify_owner_approval(self._sign(), self.ph)
            self.assertFalse(d["approved"])
            self.assertEqual(d["code"], errors.OWNER_UNREGISTERED)
        finally:
            dev2.stop_swarm()

    def test_cross_device_replay_rejected(self):
        # a signature for THIS device must not verify on ANOTHER device (same owner)
        dev2 = DeviceRuntime(name=_uniq("kvdev2"), capability_override=["compute"])
        dev2.set_owner_pubkey(self.owner_pub)
        try:
            oa = self._sign()                     # bound to self.dev.node_id
            d = dev2._verify_owner_approval(oa, self.ph)
            self.assertFalse(d["approved"])       # device_node_id in subject differs
            self.assertEqual(d["code"], errors.OWNER_SIG_INVALID)
        finally:
            dev2.stop_swarm()

    def test_resolver_priority_and_no_owner_prompt(self):
        nplan = {"action": "release"}
        ph = self.ph
        prompted = {"n": 0}
        self.dev.set_intervention_approval_callback(
            lambda plan, aid: (prompted.__setitem__("n", prompted["n"] + 1), True)[1])

        # 1. keyed sig present → keyed path, callback NOT consulted
        oa = self._sign()
        d = self.dev._resolve_plan_approval(nplan, ph, "agent", {"owner_approval": oa})
        self.assertEqual(d["kind"], "keyed")
        self.assertTrue(d["approved"])
        self.assertEqual(prompted["n"], 0)

        # 2. no sig, callback set → local path
        d = self.dev._resolve_plan_approval(nplan, ph, "agent", {})
        self.assertEqual(d["kind"], "local")
        self.assertTrue(d["approved"])
        self.assertEqual(prompted["n"], 1)

        # 3. no sig, no callback, owner key registered → PENDING (no prompt)
        self.dev._intervention_approval_callback = None
        d = self.dev._resolve_plan_approval(nplan, ph, "agent", {})
        self.assertEqual(d["kind"], "pending")
        self.assertIn("nonce", d)

        # 4. no sig, no callback, no owner key → deny
        dev2 = DeviceRuntime(name=_uniq("bare"), capability_override=["compute"])
        try:
            d = dev2._resolve_plan_approval(nplan, ph, "agent", {})
            self.assertEqual(d["kind"], "local")
            self.assertFalse(d["approved"])
        finally:
            dev2.stop_swarm()


# ── 3. end-to-end over the wire (both transports) ───────────────────────────────

class KeyedApprovalWireMixin:
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

    def _fixture_holder(self):
        fd, node = tempfile.mkstemp(prefix="k10a_node_"); os.close(fd)
        self.tmpfiles.append(node)
        child = subprocess.Popen(["python3", "-c",
                                  f"f=open({node!r},'rb'); import time; time.sleep(120)"])
        self.procs.append(child)
        time.sleep(0.4)
        return node, child

    def _bind(self, agent, device, cap):
        self._discover(agent, device, cap)
        return agent.bind_remote_to(device.node_id, cap)

    def test_keyed_approval_executes_and_audits(self):
        d = self.make_device("kx")
        owner_priv, owner_pub = crypto.generate_keypair()
        d.set_owner_pubkey(owner_pub)
        d.policy.set_approval_callback(lambda r, a: True)   # bind gate open; NO per-plan callback
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node)
        a = self.make_agent("kxag")
        r = self._bind(a, d, cap)
        self.assertTrue(r.get("verified"), r)

        plan = _release_plan(child.pid)
        # ROUND 1 → pending, device hands back the exact plan_hash to sign
        r1 = a.propose_intervention(r, plan)
        self.assertEqual(r1["status"], "pending_owner_approval")
        self.assertEqual(r1.get("code"), errors.OWNER_APPROVAL_REQUIRED)
        req = r1["owner_approval_request"]
        self.assertTrue(req["plan_hash"])
        self.assertEqual(req["device_node_id"], d.node_id)
        # child still alive — nothing mutated on round 1
        self.assertTrue(child.poll() is None)

        # OWNER signs the device-computed plan_hash; agent resubmits SAME plan
        oa = signing.sign_owner_approval(req["plan_hash"], req["device_node_id"],
                                         owner_priv, owner_pub)
        r2 = a.propose_intervention(r, plan, owner_approval=oa)
        self.assertEqual(r2["status"], "executed", r2)
        self.assertTrue(r2["executed"])
        self.assertTrue(r2["verify"]["passed"])

        # AUDIT: keyed proof recorded, and it survives a "restart" (fresh log obj)
        fresh = _audit.AuditLog(d.name, d.private_key, d.public_key)
        ok, _ = fresh.verify_chain()
        self.assertTrue(ok)
        head = fresh.head()
        self.assertEqual(head["approver"], "owner:" + crypto.derive_node_id(owner_pub))
        self.assertEqual(head["owner_pubkey"], owner_pub)
        self.assertEqual(head["owner_sig"], oa["sig"])
        self.assertTrue(head["executed"])

    def test_sig_for_one_plan_rejected_for_another(self):
        d = self.make_device("kmis")
        owner_priv, owner_pub = crypto.generate_keypair()
        d.set_owner_pubkey(owner_pub)
        d.policy.set_approval_callback(lambda r, a: True)
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node)
        a = self.make_agent("kmisag")
        r = self._bind(a, d, cap)

        plan_a = _release_plan(child.pid)
        plan_b = _release_plan(child.pid + 1)              # a DIFFERENT normalized plan
        r1 = a.propose_intervention(r, plan_a)             # get plan_a's hash
        oa = signing.sign_owner_approval(r1["owner_approval_request"]["plan_hash"],
                                         d.node_id, owner_priv, owner_pub)
        # submit plan_b with plan_a's signature → rejected, nothing runs
        resp = a.propose_intervention(r, plan_b, owner_approval=oa)
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp.get("code"), errors.OWNER_SIG_INVALID)
        self.assertFalse(resp.get("executed"))
        self.assertTrue(child.poll() is None)              # untouched

    def test_replay_rejected_over_wire(self):
        d = self.make_device("krep")
        owner_priv, owner_pub = crypto.generate_keypair()
        d.set_owner_pubkey(owner_pub)
        d.policy.set_approval_callback(lambda r, a: True)
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node)
        a = self.make_agent("krepag")
        r = self._bind(a, d, cap)

        plan = _release_plan(child.pid)
        r1 = a.propose_intervention(r, plan)
        oa = signing.sign_owner_approval(r1["owner_approval_request"]["plan_hash"],
                                         d.node_id, owner_priv, owner_pub)
        first = a.propose_intervention(r, plan, owner_approval=oa)
        self.assertEqual(first["status"], "executed", first)
        # replay the SAME owner approval → rejected on the nonce seen-cache
        replay = a.propose_intervention(r, plan, owner_approval=oa)
        self.assertEqual(replay["status"], "denied")
        self.assertEqual(replay.get("code"), errors.OWNER_APPROVAL_STALE)

    def test_unregistered_owner_falls_back_to_local_callback(self):
        # No owner key registered → Phase 8 behaviour: the local callback approves.
        d = self.make_device("kfb")
        d.policy.set_approval_callback(lambda r, a: True)
        d.set_intervention_approval_callback(lambda plan, aid: True)
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node)
        a = self.make_agent("kfbag")
        r = self._bind(a, d, cap)
        resp = a.propose_intervention(r, _release_plan(child.pid))
        self.assertEqual(resp["status"], "executed", resp)
        self.assertTrue(resp["executed"])
        head = d._audit_log().head()
        self.assertEqual(head["approver"], "device_owner@local")   # unchanged
        self.assertNotIn("owner_sig", head)


class TestKeyedApprovalLAN(KeyedApprovalWireMixin, unittest.TestCase):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass

    def make_device(self, name):
        d = DeviceRuntime(name=_uniq(f"lan-{name}"), capability_override=["compute"], lease_ttl=120)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=_uniq(f"lan-{name}"), auto_renew=False)
        a.start()
        self.agents.append(a)
        return a

    def _attach(self, device, family, target, **opts):
        return device.attach_intervention(family, target, **opts)["name"]

    def _discover(self, agent, device, cap):
        ip, port = device.swarm.address
        now = time.time()
        with agent.swarm._lock:
            for c in device.advertise():
                rec = {"node_id": device.node_id, "name": c.name, "tags": list(c.tags),
                       "live_state": dict(c.live_state), "public_key": device.public_key,
                       "address": [ip, port], "device_class": device.device_class, "ts": now}
                man = device._cap_manifest(c)
                if man is not None:
                    rec["manifest"] = man
                agent.swarm.records[(device.node_id, c.name)] = rec
        agent.swarm.add_known_peer(device.node_id, ip, port)


class TestKeyedApprovalDHT(KeyedApprovalWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="keyed-bootstrap", udp_port=free_udp_port(), ttl=30)
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
        d = DeviceRuntime(name=_uniq(f"dht-{name}"), capability_override=["compute"], lease_ttl=120)
        self._attach_dht(d)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=_uniq(f"dht-{name}"), auto_renew=False)
        self._attach_dht(a)
        a.start()
        self.agents.append(a)
        time.sleep(0.3)
        return a

    def _attach(self, device, family, target, **opts):
        name = device.attach_intervention(family, target, **opts)["name"]
        time.sleep(0.4)
        return name

    def _discover(self, agent, device, cap):
        agent.find_capability(cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
