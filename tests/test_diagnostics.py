"""
tests/test_diagnostics.py — PHASE 7: the diagnostic surface (read-only).

Diagnostics let an agent SEE a subsystem's failure state before any fix is
attempted — the read-only half of the fix loop. They are a pure extension of the
probes layer: each reads /proc, /sys, or a read-only query to a standard tool and
NEVER mutates state. All are Linux-specific and degrade gracefully where a source
is absent (observable=False + reason, never an unhandled FileNotFoundError).

Covers:
  - each diagnostic reads REAL state on this machine and returns the declared shape
  - manifest carries cannot_observe + validates; consent_tier sensitive
  - graceful degrade when a source path is absent (mocked missing /proc/modules)
  - a boolean-field condition (eq on a bool) fires on a simulated present:true→false
    transition, using a controllable FIXTURE node (a temp file), not the real camera
  - sensitive tier DENIES an unapproved remote agent; APPROVED agent binds + reads
  - the remote-read path works over BOTH transports (LAN + DHT)

Run:  python3 -m unittest tests.test_diagnostics -v
"""

import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a import manifest as _manifest
from d2a import errors
from d2a.stream_source import (
    DeviceNodeHealthSource, KernelModuleHealthSource,
    ServiceHealthSource, UsbPowerHealthSource, DIAGNOSTIC_SOURCES,
)
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent
from tests._env import use_tmp_home, restore_home


def setUpModule():
    use_tmp_home()


def tearDownModule():
    restore_home()


STEP = 0.35   # per-change settle; ~3 samples at 10 Hz


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _first_real_module() -> str | None:
    try:
        with open("/proc/modules") as f:
            line = f.readline()
        return line.split(" ", 1)[0] if line else None
    except OSError:
        return None


def _first_usb_dev() -> str | None:
    base = "/sys/bus/usb/devices"
    if not os.path.isdir(base):
        return None
    for d in sorted(os.listdir(base)):
        if os.path.isdir(os.path.join(base, d, "power")):
            return d
    return None


# ── standalone: sources read REAL state + declared shape ─────────────────────────

class TestDiagnosticSourcesReadReal(unittest.TestCase):
    """Each diagnostic reads live state on this Linux machine and returns exactly
    the fields its manifest declares (plus the shared observable/reason contract)."""

    def _assert_shape(self, family: str, reading: dict):
        self.assertIsInstance(reading, dict)
        man = _manifest.diagnostic_manifest(family, "probe")
        for field in man["reading"]:
            self.assertIn(field, reading, f"{family} reading missing declared field {field!r}")
        self.assertIn("observable", reading)
        self.assertIsInstance(reading["observable"], bool)
        self.assertIsInstance(reading["reason"], str)

    def test_device_node_health_real(self):
        # /dev/null exists on every Linux host — a stable real node to inspect.
        r = DeviceNodeHealthSource("/dev/null").read()
        self._assert_shape("device_node_health", r)
        self.assertTrue(r["present"])
        self.assertIsInstance(r["readable"], bool)
        self.assertIsInstance(r["holder_pids"], list)
        self.assertTrue(r["observable"])

    def test_kernel_module_health_real(self):
        mod = _first_real_module()
        if mod is None:
            self.skipTest("/proc/modules unreadable on this host")
        r = KernelModuleHealthSource(mod).read()
        self._assert_shape("kernel_module_health", r)
        self.assertTrue(r["loaded"], f"module {mod} from /proc/modules should read loaded")
        self.assertTrue(r["observable"])
        self.assertIsInstance(r["dmesg_lines"], list)
        # dmesg may or may not be privileged; either way we must be honest, not crash.
        self.assertIsInstance(r["dmesg_available"], bool)

    def test_service_health_real(self):
        if not __import__("shutil").which("systemctl"):
            self.skipTest("no systemctl on this host")
        r = ServiceHealthSource("systemd-journald").read()
        self._assert_shape("service_health", r)
        self.assertTrue(r["observable"])
        self.assertIn(r["active_state"], ("active", "inactive", "failed", "activating", "unknown"))
        self.assertEqual(r["active"], r["active_state"] == "active")

    def test_usb_power_health_real(self):
        dev = _first_usb_dev()
        if dev is None:
            self.skipTest("no USB device with power sysfs on this host")
        r = UsbPowerHealthSource(dev).read()
        self._assert_shape("usb_power_health", r)
        self.assertTrue(r["present"])
        self.assertTrue(r["observable"])
        self.assertEqual(r["autosuspend"], r["control"] == "auto")


# ── standalone: manifest honesty ────────────────────────────────────────────────

class TestDiagnosticManifests(unittest.TestCase):
    def test_cannot_observe_present_and_validates(self):
        for family in DIAGNOSTIC_SOURCES:
            man = _manifest.diagnostic_manifest(family, "/dev/video0")
            self.assertIn("cannot_observe", man)
            self.assertTrue(man["cannot_observe"], f"{family} must declare blind spots")
            self.assertTrue(all(isinstance(x, str) for x in man["cannot_observe"]))
            self.assertEqual(man["consent_tier"], "sensitive")
            # re-validating the built manifest against the SSOT must pass
            _manifest.validate_manifest(man, "sensitive")

    def test_cannot_observe_rejected_when_not_list_of_strings(self):
        bad = {"description": "x", "reading": {}, "consent_tier": "open",
               "cannot_observe": ["ok", 5]}
        with self.assertRaises(_manifest.ManifestError):
            _manifest.validate_manifest(bad, "open")

    def test_cannot_observe_is_not_derived_provenance(self):
        # cannot_observe is valid on a plain (non-derived) manifest; cannot_detect
        # is NOT (it is derived-only). This proves the two honesty fields are distinct.
        ok = {"description": "x", "reading": {}, "consent_tier": "open",
              "cannot_observe": ["a"]}
        self.assertEqual(_manifest.validate_manifest(ok, "open")["cannot_observe"], ["a"])
        bad = {"description": "x", "reading": {}, "consent_tier": "open",
               "cannot_detect": ["a"]}
        with self.assertRaises(_manifest.ManifestError):
            _manifest.validate_manifest(bad, "open")

    def test_unknown_family_raises(self):
        with self.assertRaises(_manifest.ManifestError):
            _manifest.diagnostic_manifest("no_such_family", "x")


# ── standalone: graceful degrade (source absent) ─────────────────────────────────

class TestDiagnosticGracefulDegrade(unittest.TestCase):
    def test_kernel_module_degrades_when_proc_absent(self):
        real_exists = os.path.exists

        def fake_exists(p):
            return False if p == "/proc/modules" else real_exists(p)

        with mock.patch("d2a.stream_source.os.path.exists", side_effect=fake_exists):
            r = KernelModuleHealthSource("anything").read()   # must NOT raise
        self.assertFalse(r["observable"], "unknown state flagged, not crashed")
        self.assertFalse(r["loaded"], "unknown modelled as safe-default False")
        self.assertIn("proc/modules", r["reason"])
        self.assertIsInstance(r["dmesg_lines"], list)

    def test_usb_degrades_when_sysfs_absent(self):
        r = UsbPowerHealthSource("99-99-nonexistent").read()   # must NOT raise
        self.assertFalse(r["observable"])
        self.assertFalse(r["present"])
        self.assertIn("absent", r["reason"])

    def test_device_node_absent_is_present_false_not_crash(self):
        r = DeviceNodeHealthSource("/dev/definitely_not_here_xyz").read()
        self.assertTrue(r["observable"], "filesystem always answers existence")
        self.assertFalse(r["present"])
        self.assertEqual(r["holder_pids"], [])

    def test_service_degrades_when_systemctl_absent(self):
        with mock.patch("d2a.stream_source.shutil.which", return_value=None):
            r = ServiceHealthSource("whatever.service").read()   # must NOT raise
        self.assertFalse(r["observable"])
        self.assertFalse(r["active"])
        self.assertIn("systemctl", r["reason"])


# ── transport-parametrized wire tests (remote read + condition + consent) ─────────

class DiagWireMixin:
    """Shared wire tests over a transport. Subclasses provide make_device/
    make_agent/_discover/_settle (mirrors the event-layer test structure)."""

    def setUp(self):
        self.devices = []
        self.agents = []
        self._tmpfiles = []
        self._setup_transport()

    def tearDown(self):
        for a in self.agents:
            try: a.stop()
            except Exception: pass
        for d in self.devices:
            try: d.stop_swarm()
            except Exception: pass
        for f in self._tmpfiles:
            try: os.remove(f)
            except OSError: pass
        self._teardown_transport()
        time.sleep(0.05)

    def _settle(self):
        pass

    def _fixture_node(self) -> str:
        """A controllable stand-in for a device node: a temp file whose existence
        the test flips to drive present:true→false (NOT the real camera)."""
        fd, path = tempfile.mkstemp(prefix="diag_fixture_")
        os.close(fd)
        self._tmpfiles.append(path)
        return path

    # attach a diagnostic and open the consent gate for approved-agent tests
    def _attach(self, device, family, target, approve=True, **opts):
        info = device.attach_diagnostic(family, target, **opts)
        if approve:
            device.policy.set_approval_callback(lambda r, a: True)
        self._settle()
        return info["name"]

    def _bind(self, agent, device, cap):
        self._discover(agent, device, cap)
        return agent.bind_remote_to(device.node_id, cap)

    # 1 — an approved remote agent binds a diagnostic and reads REAL state
    def test_remote_read_real_state(self):
        d = self.make_device("diagdev")
        cap = self._attach(d, "device_node_health", "/dev/null")
        a = self.make_agent("diagag", auto_renew=False)
        r = self._bind(a, d, cap)
        self.assertTrue(r.get("verified"), f"approved bind should verify: {r}")

        resp = a.request_data(r)
        self.assertEqual(resp["type"], "reading")
        node = resp["frame"]["raw"]["device_node_health"]
        self.assertTrue(node["present"])
        self.assertIn("readable", node)
        self.assertIn("holder_pids", node)

    # 2 — the manifest rides on-wire; the agent can describe() its blind spots
    def test_manifest_on_wire(self):
        d = self.make_device("mandev")
        cap = self._attach(d, "usb_power_health", _first_usb_dev() or "1-1")
        a = self.make_agent("manag", auto_renew=False)
        self._discover(a, d, cap)
        man = a.describe(cap, d.node_id)
        self.assertIsNotNone(man, "diagnostic manifest must be discoverable on-wire")
        self.assertIn("cannot_observe", man)
        self.assertEqual(man["consent_tier"], "sensitive")

    # 3 — boolean-field condition (eq on bool) fires on a present:true→false edge
    def test_bool_condition_fires_on_transition(self):
        node = self._fixture_node()
        d = self.make_device("condev")
        cap = self._attach(d, "device_node_health", node)
        a = self.make_agent("conag", auto_renew=False)
        r = self._bind(a, d, cap)
        self.assertTrue(r.get("verified"))

        events = []
        resp = a.on_event(r, {"field": "present", "op": "eq", "value": False},
                          lambda e: events.append(e), eval_hz=10)
        self.assertEqual(resp.get("status"), "subscribed", f"subscribe failed: {resp}")

        time.sleep(STEP)                 # baseline: present=True (eq False → false) — no fire
        self.assertEqual(events, [], "baseline must not fire")
        os.remove(node); self._tmpfiles.remove(node)   # present flips True→False
        time.sleep(2 * STEP)
        self.assertEqual(len(events), 1, "eq-on-bool must fire on the true→false crossing")
        fired = events[0]["reading"]["raw"]["device_node_health"]
        self.assertFalse(fired["present"], "triggering snapshot shows present=False")

    # 4 — sensitive tier DENIES an unapproved remote agent
    def test_sensitive_denies_unapproved_agent(self):
        d = self.make_device("denydev")
        cap = self._attach(d, "service_health", "systemd-journald", approve=False)
        a = self.make_agent("denyag", auto_renew=False)
        r = self._bind(a, d, cap)
        self.assertEqual(r.get("status"), "denied")
        self.assertEqual(r.get("code"), errors.APPROVAL_REQUIRED)
        self.assertFalse(r.get("verified"))


# ── LAN concrete ─────────────────────────────────────────────────────────────────

class TestDiagnosticsLAN(DiagWireMixin, unittest.TestCase):
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
                rec = {
                    "node_id": device.node_id, "name": c.name,
                    "tags": list(c.tags), "live_state": dict(c.live_state),
                    "public_key": device.public_key, "address": [ip, port],
                    "device_class": device.device_class, "ts": now,
                }
                man = device._capability_record(c, ip, port).get("manifest")
                if man is not None:
                    rec["manifest"] = man
                agent.swarm.records[(device.node_id, c.name)] = rec
        agent.swarm.add_known_peer(device.node_id, ip, port)


# ── DHT concrete ─────────────────────────────────────────────────────────────────

class TestDiagnosticsDHT(DiagWireMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="diag-bootstrap", udp_port=free_udp_port(), ttl=30)
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

    def _settle(self):
        time.sleep(0.4)

    def _discover(self, agent, device, cap):
        agent.find_capability(cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
