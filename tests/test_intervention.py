"""
tests/test_intervention.py — PHASE 8: the intervention (mutating) layer.

The fix loop: diagnose (Phase 7) → PLAN → owner APPROVE → EXECUTE → device-run
VERIFY → signed AUDIT. A wrong intervention has no undo, so the safety net is
owner-approval-of-a-PLAN plus a tamper-evident audit trail (not git). Everything
here mutates REAL state on this Linux machine through SAFE, reversible-or-labelled
reference fixers — no camera, nothing high-stakes-irreversible.

Covers (the full Phase 8 list + the added assertions):
  - double gate L1: intervention tier DENIES a remote agent with no bind approval
  - double gate L2: approved bind + DENIED plan leaves state untouched + audits denial
  - approved plan executes + DEVICE runs the declared verify + reports pass
  - a plan whose verify FAILS after execution → failed_verify (never silent success)
  - reference intervention actually CHANGES + REVERSES real state (user service) and
    the diagnostic confirms it
  - process_release reversible:false is SURFACED to the approver (approver saw it)
  - kernel_module_intervene is privilege-gated → refused_preflight here (real test skipped)
  - signed audit entry written + survives restart + append-only (prior immutable)
  - audit chain refuses to extend after a tampered last line (FAIL-CLOSED)
  - plan validation: mandatory fields, reversible:false needs ack, verify validated
    against the paired diagnostic's manifest
  - both transports (LAN + DHT)

Run:  python3 -m unittest tests.test_intervention -v
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a import crypto, errors
from d2a import manifest as _manifest
from d2a import audit as _audit
from d2a.stream_source import DeviceNodeHealthSource, ServiceHealthSource
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


def _user_systemd_ok() -> bool:
    if not __import__("shutil").which("systemctl"):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "show-environment"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


_UNIT_TXT = "[Unit]\nDescription=D2A phase8 test unit\n[Service]\nExecStart=/bin/sleep 3600\n"


def _make_user_unit() -> str:
    """Create a PERSISTENT user unit (so start/stop/start cycles work) and return
    its name. Caller must _remove_user_unit() it."""
    base = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    os.makedirs(base, exist_ok=True)
    name = f"d2a-p8-{os.getpid()}-{int(time.time()*1000)%100000}.service"
    with open(os.path.join(base, name), "w") as f:
        f.write(_UNIT_TXT)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return name


def _remove_user_unit(name: str) -> None:
    base = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    subprocess.run(["systemctl", "--user", "stop", name], capture_output=True)
    try:
        os.remove(os.path.join(base, name))
    except OSError:
        pass
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def _svc_active(name: str) -> bool:
    return ServiceHealthSource(name, user=True).read().get("active") is True


def _svc_plan(action: str, want_active: bool) -> dict:
    inverse = "stop" if action != "stop" else "start"
    return {
        "action": action, "params": {},
        "evidence": {"diagnostic": "service_health", "field": "active",
                     "reading": {"active": not want_active}},
        "expected": f"unit becomes active={want_active}",
        "verify": {"diagnostic": "service_health",
                   "condition": {"field": "active", "op": "eq", "value": want_active}},
        "reversible": True, "reversible_how": f"systemctl --user {inverse} <unit>",
    }


# ── standalone: audit log ────────────────────────────────────────────────────────

class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._old = os.environ.get("D2A_HOME")
        os.environ["D2A_HOME"] = self.home
        self.kp = crypto.load_or_create_keypair("audit-unit")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("D2A_HOME", None)
        else:
            os.environ["D2A_HOME"] = self._old

    def _log(self):
        return _audit.AuditLog("audit-unit", self.kp.private_key, self.kp.public_key)

    def test_signed_appendonly_survives_restart(self):
        log = self._log()
        e0 = log.append({"result_status": "executed", "plan": {"action": "a"}})
        e1 = log.append({"result_status": "denied", "plan": {"action": "b"}})
        self.assertEqual([e0["seq"], e1["seq"]], [0, 1])
        self.assertTrue(crypto.verify_dict(e0, self.kp.public_key))    # device-signed
        self.assertEqual(e1["prev_hash"], _audit._entry_hash(e0))       # chained
        # a FRESH log object on the same file (restart) continues the chain
        log2 = self._log()
        e2 = log2.append({"result_status": "failed_verify", "plan": {"action": "c"}})
        self.assertEqual(e2["seq"], 2)
        ok, _ = log2.verify_chain()
        self.assertTrue(ok)
        self.assertEqual(len(log2.entries()), 3)

    def test_prior_entries_immutable_and_failclosed_on_tamper(self):
        log = self._log()
        log.append({"result_status": "executed", "plan": {"action": "a"}})
        log.append({"result_status": "executed", "plan": {"action": "b"}})
        # tamper a signed field in the LAST line
        lines = open(log.path).read().splitlines()
        rec = json.loads(lines[-1]); rec["result_status"] = "denied"
        lines[-1] = json.dumps(rec, separators=(",", ":"))
        open(log.path, "w").write("\n".join(lines) + "\n")
        ok, detail = log.verify_chain()
        self.assertFalse(ok, "signature over the tampered line must fail")
        # FAIL-CLOSED: refuse to extend a tampered log
        with self.assertRaises(_audit.AuditError):
            self._log().append({"result_status": "executed", "plan": {"action": "c"}})

    def test_midchain_tamper_breaks_chain(self):
        log = self._log()
        for i in range(3):
            log.append({"result_status": "executed", "plan": {"i": i}})
        lines = open(log.path).read().splitlines()
        rec = json.loads(lines[0]); rec["result_status"] = "x"
        lines[0] = json.dumps(rec, separators=(",", ":"))
        open(log.path, "w").write("\n".join(lines) + "\n")
        ok, _ = log.verify_chain()
        self.assertFalse(ok)


# ── standalone: manifest + plan honesty ─────────────────────────────────────────

class TestInterventionManifests(unittest.TestCase):
    def test_cannot_fix_present_and_validates(self):
        for fam in ("service_intervene", "process_release", "kernel_module_intervene"):
            man = _manifest.intervention_manifest(fam, "target")
            self.assertEqual(man["consent_tier"], "intervention")
            self.assertTrue(man["cannot_fix"])
            self.assertTrue(all(isinstance(x, str) for x in man["cannot_fix"]))
            self.assertTrue(any(a.get("mutating") for a in man["actions"].values()),
                            "at least one action marked mutating")
            _manifest.validate_manifest(man, "intervention")

    def test_unknown_family_raises(self):
        with self.assertRaises(_manifest.ManifestError):
            _manifest.intervention_manifest("nope", "x")

    def test_intervention_tier_in_vocabulary(self):
        self.assertIn("intervention", _manifest._CONSENT_TIERS)


class TestPlanValidation(unittest.TestCase):
    """_validate_plan is device-side; exercise it directly (no transport)."""

    def setUp(self):
        self.d = DeviceRuntime(name="planval", capability_override=["compute"])
        self.cap = self.d.attach_intervention("process_release", "/tmp/x_node")["name"]

    def tearDown(self):
        try: self.d.stop_swarm()
        except Exception: pass

    def _ok(self, plan):
        return self.d._validate_plan(self.cap, plan)

    def _base_release(self, **over):
        p = {"action": "release", "params": {"pid": 12345},
             "evidence": {"field": "holder_count"}, "expected": "released",
             "verify": {"diagnostic": "device_node_health",
                        "condition": {"field": "holder_count", "op": "eq", "value": 0}},
             "reversible": False, "reversible_how": "", "reversible_ack": True}
        p.update(over)
        return p

    def test_valid_plan(self):
        ok, why, norm = self._ok(self._base_release())
        self.assertTrue(ok, why)
        self.assertEqual(norm["action"], "release")

    def test_reversible_false_requires_ack(self):
        ok, why, _ = self._ok(self._base_release(reversible_ack=False))
        self.assertFalse(ok)
        self.assertIn("reversible_ack", why)

    def test_missing_evidence_rejected(self):
        p = self._base_release(); p.pop("evidence")
        ok, why, _ = self._ok(p)
        self.assertFalse(ok); self.assertIn("evidence", why)

    def test_verify_condition_validated_against_paired_diagnostic(self):
        # a field not in device_node_health's manifest must be rejected
        bad = self._base_release(verify={"diagnostic": "device_node_health",
                                          "condition": {"field": "nope", "op": "eq", "value": 0}})
        ok, why, _ = self._ok(bad)
        self.assertFalse(ok); self.assertIn("verify.condition", why)

    def test_unknown_action_rejected(self):
        ok, why, _ = self._ok(self._base_release(action="frobnicate"))
        self.assertFalse(ok); self.assertIn("unknown action", why)

    def test_changed_op_rejected_for_verify(self):
        bad = self._base_release(verify={"diagnostic": "device_node_health",
                                         "condition": {"field": "holder_count", "op": "changed"}})
        ok, why, _ = self._ok(bad)
        self.assertFalse(ok)


# ── transport-parametrized wire tests ────────────────────────────────────────────

class InterventionWireMixin:
    def setUp(self):
        self.devices, self.agents, self.units, self.tmpfiles, self.procs = [], [], [], [], []
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
        for u in self.units:
            _remove_user_unit(u)
        for f in self.tmpfiles:
            try: os.remove(f)
            except OSError: pass
        self._teardown_transport()
        time.sleep(0.05)

    def _settle(self):
        pass

    def _discover(self, agent, device, cap):
        raise NotImplementedError

    def _attach(self, device, family, target, **opts):
        name = device.attach_intervention(family, target, **opts)["name"]
        self._settle()          # let the published record propagate (DHT)
        return name

    def _bind(self, agent, device, cap):
        self._discover(agent, device, cap)
        return agent.bind_remote_to(device.node_id, cap)

    def _new_unit(self):
        name = _make_user_unit(); self.units.append(name); return name

    def _fixture_holder(self):
        """A temp 'device node' + a child process that keeps it open."""
        fd, node = tempfile.mkstemp(prefix="p8_node_"); os.close(fd)
        self.tmpfiles.append(node)
        child = subprocess.Popen(["python3", "-c",
                                  f"f=open({node!r},'rb'); import time; time.sleep(120)"])
        self.procs.append(child)
        time.sleep(0.4)
        return node, child

    # 1 — double gate L1: bind DENIED with no owner bind-approval
    def test_bind_denied_without_approval(self):
        d = self.make_device("gate1")
        unit = self._new_unit()
        cap = self._attach(d, "service_intervene", unit)
        a = self.make_agent("gate1ag")
        r = self._bind(a, d, cap)                    # no policy approval callback set
        self.assertEqual(r.get("status"), "denied")
        self.assertEqual(r.get("code"), errors.APPROVAL_REQUIRED)
        self.assertFalse(r.get("verified"))

    # 2 — double gate L2 + owner-declines: approved bind, DENIED plan, state untouched, audited
    def test_approved_bind_denied_plan_untouched_and_audited(self):
        d = self.make_device("gate2")
        unit = self._new_unit()
        subprocess.run(["systemctl", "--user", "start", unit], capture_output=True)
        self.assertTrue(_svc_active(unit))
        cap = self._attach(d, "service_intervene", unit)
        d.policy.set_approval_callback(lambda res, aid: True)      # bind gate open
        # NO per-plan approval → default DENY
        a = self.make_agent("gate2ag")
        r = self._bind(a, d, cap)
        self.assertTrue(r.get("verified"), r)

        resp = a.propose_intervention(r, _svc_plan("stop", want_active=False))
        self.assertEqual(resp.get("status"), "denied")
        self.assertEqual(resp.get("code"), errors.APPROVAL_REQUIRED)
        self.assertFalse(resp.get("executed"))
        self.assertTrue(_svc_active(unit), "state untouched — the stop never ran")
        # the denial is audited
        head = d._audit_log().head()
        self.assertEqual(head["result_status"], "denied")
        self.assertFalse(head["approved"])
        self.assertFalse(head["executed"])

    # 3 + 5 — approved plan executes, device verify passes, and REVERSES real state
    def test_execute_verify_and_reverse_real_state(self):
        d = self.make_device("fixloop")
        unit = self._new_unit()
        subprocess.run(["systemctl", "--user", "start", unit], capture_output=True)
        cap = self._attach(d, "service_intervene", unit)
        d.policy.set_approval_callback(lambda res, aid: True)
        d.set_intervention_approval_callback(lambda plan, aid: True)
        a = self.make_agent("fixloopag")
        r = self._bind(a, d, cap)
        self.assertTrue(r.get("verified"), r)

        # CHANGE: stop → device verifies inactive
        stop = a.propose_intervention(r, _svc_plan("stop", want_active=False))
        self.assertEqual(stop["status"], "executed", stop)
        self.assertTrue(stop["executed"] and stop["verify"]["passed"])
        self.assertFalse(_svc_active(unit), "diagnostic confirms the unit stopped")

        # REVERSE: start → device verifies active again
        start = a.propose_intervention(r, _svc_plan("start", want_active=True))
        self.assertEqual(start["status"], "executed", start)
        self.assertTrue(start["verify"]["passed"])
        self.assertTrue(_svc_active(unit), "diagnostic confirms the unit restarted")

        # both audited, chain intact, seqs monotonic
        ok, _ = d._audit_log().verify_chain()
        self.assertTrue(ok)
        seqs = [e["seq"] for e in d._audit_log().entries()]
        self.assertEqual(seqs, list(range(len(seqs))))

    # 4 — a plan whose verify FAILS after execution → failed_verify (NOT silent success)
    def test_failed_verify_not_silent_success(self):
        d = self.make_device("failver")
        unit = self._new_unit()
        subprocess.run(["systemctl", "--user", "start", unit], capture_output=True)
        cap = self._attach(d, "service_intervene", unit)
        d.policy.set_approval_callback(lambda res, aid: True)
        d.set_intervention_approval_callback(lambda plan, aid: True)
        a = self.make_agent("failverag")
        r = self._bind(a, d, cap)

        # action stops the unit, but the plan DECLARES verify active==true → cannot hold
        plan = _svc_plan("stop", want_active=True)
        resp = a.propose_intervention(r, plan)
        self.assertEqual(resp["status"], "failed_verify",
                         "the fix ran but its verify failed — must not report success")
        self.assertTrue(resp["executed"], "the mutation DID run")
        self.assertFalse(resp["verify"]["passed"])
        self.assertEqual(resp.get("code"), errors.INTERVENTION_VERIFY_FAILED)
        self.assertFalse(_svc_active(unit), "the stop really happened")
        head = d._audit_log().head()
        self.assertEqual(head["result_status"], "failed_verify")
        self.assertEqual(head["verify_outcome"], "fail")

    # 6 — process_release: reversible:false SURFACED to the approver + real release
    def test_process_release_irreversible_surfaced(self):
        d = self.make_device("procrel")
        node, child = self._fixture_holder()
        self.assertEqual(DeviceNodeHealthSource(node).read()["holder_count"], 1,
                         "the child holds the fixture node")
        cap = self._attach(d, "process_release", node)
        d.policy.set_approval_callback(lambda res, aid: True)

        seen = {}
        d.set_intervention_approval_callback(lambda plan, aid: (seen.update(plan), True)[1])
        a = self.make_agent("procrelag")
        r = self._bind(a, d, cap)
        self.assertTrue(r.get("verified"), r)

        plan = {"action": "release", "params": {"pid": child.pid},
                "evidence": {"field": "holder_count", "reading": {"holder_count": 1}},
                "expected": "node released (0 holders)",
                "verify": {"diagnostic": "device_node_health",
                           "condition": {"field": "holder_count", "op": "eq", "value": 0}},
                "reversible": False, "reversible_how": "", "reversible_ack": True}
        resp = a.propose_intervention(r, plan)
        self.assertEqual(resp["status"], "executed", resp)
        self.assertTrue(resp["verify"]["passed"])
        self.assertFalse(resp["reversible"], "response labels the fix irreversible")
        # the APPROVER actually received reversible:false + the ack
        self.assertIs(seen.get("reversible"), False)
        self.assertIs(seen.get("reversible_ack"), True)
        self.assertEqual(DeviceNodeHealthSource(node).read()["holder_count"], 0,
                         "diagnostic confirms the node was released")

    # 7 — kernel_module_intervene is privilege-gated → refused_preflight here
    def test_kernel_module_privilege_gated(self):
        if os.geteuid() == 0:
            self.skipTest("running as root — module load would actually execute")
        d = self.make_device("modgate")
        cap = self._attach(d, "kernel_module_intervene", "d2a_no_such_module")
        d.policy.set_approval_callback(lambda res, aid: True)
        d.set_intervention_approval_callback(lambda plan, aid: True)   # would approve — but preflight refuses first
        a = self.make_agent("modgateag")
        r = self._bind(a, d, cap)
        plan = {"action": "load", "params": {},
                "evidence": {"field": "loaded"}, "expected": "module loaded",
                "verify": {"diagnostic": "kernel_module_health",
                           "condition": {"field": "loaded", "op": "eq", "value": True}},
                "reversible": True, "reversible_how": "modprobe -r d2a_no_such_module"}
        resp = a.propose_intervention(r, plan)
        self.assertEqual(resp["status"], "refused_preflight")
        self.assertEqual(resp.get("code"), errors.INTERVENTION_PREFLIGHT_REFUSED)
        self.assertFalse(resp["executed"], "nothing mutated — refused before executing")

    # 8 — FAIL-CLOSED at the verb: a tampered audit tail refuses further proposals
    def test_failclosed_propose_refused_after_tamper(self):
        d = self.make_device("failclosed")
        unit = self._new_unit()
        subprocess.run(["systemctl", "--user", "start", unit], capture_output=True)
        cap = self._attach(d, "service_intervene", unit)
        d.policy.set_approval_callback(lambda res, aid: True)
        d.set_intervention_approval_callback(lambda plan, aid: True)
        a = self.make_agent("failclosedag")
        r = self._bind(a, d, cap)

        # a cheap first success (stop an already-inactive unit → verify inactive)
        ok = a.propose_intervention(r, _svc_plan("stop", want_active=False))
        self.assertEqual(ok["status"], "executed", ok)

        # tamper the audit log's last line, then propose again
        path = d._audit_log().path
        lines = open(path).read().splitlines()
        rec = json.loads(lines[-1]); rec["approved"] = False
        lines[-1] = json.dumps(rec, separators=(",", ":"))
        open(path, "w").write("\n".join(lines) + "\n")

        n_before = len(d._audit_log().entries())      # 1 (tampered) line on disk
        resp = a.propose_intervention(r, _svc_plan("restart", want_active=True))
        self.assertEqual(resp.get("code"), errors.AUDIT_SEALED,
                         "device refuses to intervene over a tampered audit chain")
        self.assertEqual(len(d._audit_log().entries()), n_before,
                         "sealed path executed nothing and wrote no audit entry")


# ── LAN concrete ─────────────────────────────────────────────────────────────────

@unittest.skipUnless(_user_systemd_ok(), "no user-scope systemd bus available")
class TestInterventionLAN(InterventionWireMixin, unittest.TestCase):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass

    def make_device(self, name):
        # Namespace by transport so the LAN/DHT classes never share a device
        # identity OR audit file (both keyed by device name under one D2A_HOME).
        d = DeviceRuntime(name=f"lan-{name}", capability_override=["compute"], lease_ttl=120)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=f"lan-{name}", auto_renew=False)
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

@unittest.skipUnless(_user_systemd_ok(), "no user-scope systemd bus available")
class TestInterventionDHT(InterventionWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="intv-bootstrap", udp_port=free_udp_port(), ttl=30)
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
        d = DeviceRuntime(name=f"dht-{name}", capability_override=["compute"], lease_ttl=120)
        self._attach_dht(d)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)
        return d

    def make_agent(self, name):
        a = RemoteAgent(name=f"dht-{name}", auto_renew=False)
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
