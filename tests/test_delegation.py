"""
tests/test_delegation.py — PHASE 10B: lease delegation.

Agent A hands a binding to agent B. The core safety rule is CONSENT IS NOT
LAUNDERED: B inherits A's tier, never a fresh grant. So the device RE-GATES B at
delegation time (open passes; sensitive re-checks B through the same policy gate;
intervention requires a KEYED owner approval naming B — Phase 10A), caps B's child
lease to A's remaining lease (never longer), optionally narrows scope to a subset
of actions (never wider), makes the child NON-renewable, and tears it down when
A's lease ends (cascade) or A revokes.

Coverage:
  - B uses a delegated binding within A's remaining lease
  - cascade: B's right dies when A releases AND when A's lease expires
  - A revokes → B cut off immediately
  - sensitive re-gates on B (denied when needs_approval, allowed when owner-opened)
  - intervention requires keyed owner approval for B (no laundering: denied without;
    allowed with; foreign owner key rejected; audited)
  - scope-narrowing enforced (B cannot invoke an action outside its scope; a scope
    wider than the capability is rejected at delegation time)
  - child lease capped to the parent (a longer sub_ttl is clamped)
  - child is non-renewable; no re-delegation (B cannot delegate its child)
  - teardown via the unified path; both transports

Keys / pins / owner-pins / audit isolated to a tmpdir (never ~/.d2a).
"""

import os
import socket
import subprocess
import tempfile
import time
import unittest

from d2a import crypto, signing, errors
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
        "expected": "released",
        "verify": {"diagnostic": "device_node_health",
                   "condition": {"field": "holder_count", "op": "eq", "value": 0}},
        "reversible": False, "reversible_how": "", "reversible_ack": True,
    }


class DelegationWireMixin:
    LEASE_TTL = 300

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
        fd, node = tempfile.mkstemp(prefix="del_node_"); os.close(fd)
        self.tmpfiles.append(node)
        child = subprocess.Popen(["python3", "-c",
                                  f"f=open({node!r},'rb'); import time; time.sleep(120)"])
        self.procs.append(child)
        time.sleep(0.4)
        return node, child

    def _bind(self, agent, device, cap):
        self._discover(agent, device, cap)
        return agent.bind_remote_to(device.node_id, cap)

    # ── open-tier delegation ────────────────────────────────────────────────

    def test_delegate_use_and_revoke(self):
        d = self.make_device("d1", ["compute", "sensing"])
        A = self.make_agent("A1"); B = self.make_agent("B1")
        self._discover(A, d, "compute"); self._discover(B, d, "compute")
        r = A.bind_remote_to(d.node_id, "compute")
        self.assertTrue(r.get("verified"), r)

        res = A.delegate_binding(r, B.agent_id)
        self.assertEqual(res["status"], "delegated", res)
        self.assertIsNot(res.get("verified"), False)   # signed response verified
        bnd = B.accept_delegation(res)

        # B reads through its own child binding
        frame = B.request_data(bnd)
        self.assertEqual(frame.get("type"), "reading")
        self.assertEqual(frame.get("capability"), "compute")

        # A revokes → B is cut off immediately
        rev = A.revoke_delegation(d.node_id, bnd["binding_id"])
        self.assertEqual(rev["status"], "revoked")
        after = B.request_data(bnd)
        self.assertEqual(after.get("code"), errors.BINDING_INVALID_OR_OUT_OF_SCOPE)

    def test_child_lease_capped_to_parent(self):
        d = self.make_device("d2", ["compute"])
        A = self.make_agent("A2"); B = self.make_agent("B2")
        r = self._bind(A, d, "compute")
        parent_expiry = r["lease_expires_at"]
        # ask for a WILDLY longer sub-lease → must be clamped to the parent's
        res = A.delegate_binding(r, B.agent_id, sub_ttl=10_000_000)
        self.assertEqual(res["status"], "delegated")
        self.assertLessEqual(res["lease_expires_at"], parent_expiry + 0.001)

    def test_cascade_on_parent_release(self):
        d = self.make_device("d3", ["compute"])
        A = self.make_agent("A3"); B = self.make_agent("B3")
        r = self._bind(A, d, "compute")
        res = A.delegate_binding(r, B.agent_id)
        bnd = B.accept_delegation(res)
        self.assertEqual(B.request_data(bnd).get("type"), "reading")
        # A releases the PARENT → child cascades dead through the unified path
        A.swarm.send_and_recv(d.node_id, signing.sign_message(
            {"type": "release_binding", "from_node": A.agent_id, "capability_name": "compute"},
            A.private_key, A.public_key))
        time.sleep(0.2)
        self.assertEqual(B.request_data(bnd).get("code"),
                         errors.BINDING_INVALID_OR_OUT_OF_SCOPE)

    def test_cascade_on_parent_lease_expiry(self):
        # The lease-cascade guarantee under a TIMED expiry: give the parent a short
        # lease, wait past it, and confirm the sweeper tears the child down too —
        # B's derived right cannot outlive A's lease even if A never releases.
        self.LEASE_TTL = 2
        d = self.make_device("dexp", ["compute"])
        A = self.make_agent("Aexp"); B = self.make_agent("Bexp")
        r = self._bind(A, d, "compute")
        res = A.delegate_binding(r, B.agent_id)
        self.assertEqual(res["status"], "delegated")
        child_id = res["binding_id"]
        bnd = B.accept_delegation(res)
        self.assertEqual(B.request_data(bnd).get("type"), "reading")   # works while A's lease is live

        # Wait past the parent lease (2s) + a sweeper tick (interval min(ttl/10,5)=0.2s).
        time.sleep(3.5)

        # A's parent is gone AND the child cascaded dead — B is cut off.
        self.assertNotEqual(d.broker.get_binding(child_id).status, "active")
        after = B.request_data(bnd)
        self.assertEqual(after.get("code"), errors.BINDING_INVALID_OR_OUT_OF_SCOPE)

    def test_child_non_renewable(self):
        d = self.make_device("d4", ["compute"])
        A = self.make_agent("A4"); B = self.make_agent("B4")
        r = self._bind(A, d, "compute")
        res = A.delegate_binding(r, B.agent_id)
        child_id = res["binding_id"]
        # B tries to renew its child directly → refused (not the owner of a renewable lease)
        renew = B.swarm.send_and_recv(d.node_id, signing.sign_message(
            {"type": "renew_binding", "from_node": B.agent_id,
             "binding_id": child_id, "capability_name": "compute"},
            B.private_key, B.public_key))
        self.assertEqual(renew.get("status"), "denied")
        self.assertEqual(renew.get("code"), errors.NOT_OWNER)

    def test_no_redelegation(self):
        d = self.make_device("d5", ["compute"])
        A = self.make_agent("A5"); B = self.make_agent("B5"); C = self.make_agent("C5")
        r = self._bind(A, d, "compute")
        res = A.delegate_binding(r, B.agent_id)
        bnd = B.accept_delegation(res)
        # B attempts to re-delegate its child to C → refused
        redele = B.delegate_binding(
            {"binding_id": bnd["binding_id"], "provider_node_id": d.node_id,
             "capability_name": "compute"}, C.agent_id)
        self.assertEqual(redele.get("status"), "denied")
        self.assertEqual(redele.get("code"), errors.NOT_DELEGATOR)

    def test_delegate_requires_ownership(self):
        # An agent that does NOT own the parent cannot delegate it.
        d = self.make_device("d6", ["compute"])
        A = self.make_agent("A6"); B = self.make_agent("B6")
        r = self._bind(A, d, "compute")
        # B forges a delegate request naming A's binding as parent
        forged = B.swarm.send_and_recv(d.node_id, signing.sign_message(
            {"type": "delegate_binding", "from_node": B.agent_id,
             "parent_binding_id": r["binding_id"], "capability": "compute",
             "delegate_agent_id": B.agent_id}, B.private_key, B.public_key))
        self.assertEqual(forged.get("status"), "denied")
        self.assertEqual(forged.get("code"), errors.NOT_DELEGATOR)

    # ── sensitive-tier re-gating ─────────────────────────────────────────────

    def test_sensitive_regates_on_delegate(self):
        d = self.make_device("d7", ["compute", "camera"])
        d.policy.allow("camera")                       # owner opens camera for binding
        A = self.make_agent("A7"); B = self.make_agent("B7")
        r = self._bind(A, d, "camera")
        self.assertTrue(r.get("verified"), r)
        # while camera is policy-allowed, delegation to B passes the re-gate
        ok = A.delegate_binding(r, B.agent_id)
        self.assertEqual(ok["status"], "delegated")
        # owner closes camera again → a NEW delegation to B is re-gated and denied
        d.policy.require_approval("camera")
        no = A.delegate_binding(r, B.agent_id)
        self.assertEqual(no["status"], "denied")
        self.assertEqual(no["code"], errors.APPROVAL_REQUIRED)

    # ── intervention-tier: keyed owner approval, no laundering ───────────────

    def _intv_setup(self, tag):
        d = self.make_device(tag, ["compute"])
        owner_priv, owner_pub = crypto.generate_keypair()
        d.set_owner_pubkey(owner_pub)
        d.policy.set_approval_callback(lambda r, a: True)   # bind gate open for A
        node, child = self._fixture_holder()
        cap = self._attach(d, "process_release", node)
        A = self.make_agent(tag + "A"); B = self.make_agent(tag + "B")
        r = self._bind(A, d, cap)
        self.assertTrue(r.get("verified"), r)
        return d, A, B, r, cap, node, child, owner_priv, owner_pub

    def test_intervention_delegation_requires_keyed_owner_approval(self):
        d, A, B, r, cap, node, child, opriv, opub = self._intv_setup("iv1")
        # no owner approval attached → denied (no laundering)
        no = A.delegate_binding(r, B.agent_id)
        self.assertEqual(no["status"], "denied")
        self.assertEqual(no["code"], errors.OWNER_APPROVAL_REQUIRED)

        # owner signs a delegation approval NAMING B → delegated + audited
        oa = signing.sign_delegation_approval(cap, B.agent_id, r["binding_id"],
                                              d.node_id, opriv, opub)
        yes = A.delegate_binding(r, B.agent_id, owner_approval=oa)
        self.assertEqual(yes["status"], "delegated", yes)
        head = d._audit_log().head()
        self.assertEqual(head["kind"], "delegation")
        self.assertEqual(head["delegate"], B.agent_id)
        self.assertEqual(head["owner_pubkey"], opub)
        self.assertTrue(d._audit_log().verify_chain()[0])

    def test_intervention_delegation_foreign_owner_rejected(self):
        d, A, B, r, cap, node, child, opriv, opub = self._intv_setup("iv2")
        other_priv, other_pub = crypto.generate_keypair()
        oa = signing.sign_delegation_approval(cap, B.agent_id, r["binding_id"],
                                              d.node_id, other_priv, other_pub)
        resp = A.delegate_binding(r, B.agent_id, owner_approval=oa)
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], errors.OWNER_KEY_MISMATCH)

    def test_intervention_delegation_approval_bound_to_delegate(self):
        # An owner approval naming agent X must not authorize delegating to agent Y.
        d, A, B, r, cap, node, child, opriv, opub = self._intv_setup("iv3")
        C = self.make_agent("iv3C")
        oa_for_B = signing.sign_delegation_approval(cap, B.agent_id, r["binding_id"],
                                                    d.node_id, opriv, opub)
        # try to use B's approval to delegate to C → subject mismatch → invalid
        resp = A.delegate_binding(r, C.agent_id, owner_approval=oa_for_B)
        self.assertEqual(resp["status"], "denied")
        self.assertEqual(resp["code"], errors.OWNER_SIG_INVALID)

    def test_scope_narrowing_enforced(self):
        d, A, B, r, cap, node, child, opriv, opub = self._intv_setup("iv4")
        oa = signing.sign_delegation_approval(cap, B.agent_id, r["binding_id"],
                                              d.node_id, opriv, opub)
        # delegate with an EMPTY action allow-list → B may propose nothing
        res = A.delegate_binding(r, B.agent_id, scope={"actions": []}, owner_approval=oa)
        self.assertEqual(res["status"], "delegated")
        self.assertEqual(res["scope"], {"actions": []})
        bnd = B.accept_delegation(res)
        pr = B.propose_intervention(bnd, _release_plan(child.pid))
        self.assertEqual(pr.get("code"), errors.DELEGATION_SCOPE_EXCEEDED)

    def test_scope_wider_than_capability_rejected(self):
        d, A, B, r, cap, node, child, opriv, opub = self._intv_setup("iv5")
        oa = signing.sign_delegation_approval(cap, B.agent_id, r["binding_id"],
                                              d.node_id, opriv, opub)
        res = A.delegate_binding(r, B.agent_id,
                                 scope={"actions": ["release", "frobnicate"]},
                                 owner_approval=oa)
        self.assertEqual(res["status"], "denied")
        self.assertEqual(res["code"], errors.DELEGATION_SCOPE_EXCEEDED)


class TestDelegationLAN(DelegationWireMixin, unittest.TestCase):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass

    def make_device(self, name, caps):
        d = DeviceRuntime(name=_uniq(f"lan-{name}"), capability_override=caps,
                          lease_ttl=self.LEASE_TTL)
        d.start_swarm()
        self.devices.append(d)
        self._last_device = d
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=_uniq(f"lan-{name}"), auto_renew=False)
        a.start()
        self.agents.append(a)
        # seed the agent's cache with whatever the most-recent device advertises
        self._seed(a, self._last_device)
        return a

    def _attach(self, device, family, target, **opts):
        name = device.attach_intervention(family, target, **opts)["name"]
        for a in self.agents:
            self._seed(a, device)
        return name

    def _seed(self, agent, device):
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

    def _discover(self, agent, device, cap):
        self._seed(agent, device)


class TestDelegationDHT(DelegationWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="deleg-bootstrap", udp_port=free_udp_port(), ttl=30)
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
        d = DeviceRuntime(name=_uniq(f"dht-{name}"), capability_override=caps,
                          lease_ttl=self.LEASE_TTL)
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
