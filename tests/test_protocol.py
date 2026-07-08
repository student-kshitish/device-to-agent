"""
tests/test_protocol.py — wire protocol version negotiation (v1.0).

Pure stdlib unittest. The version logic lives at the serialization chokepoints,
so wire tests run on both LANSwarm and DHTSwarm.

Covers:
  - helpers: major_of / versions_compatible / classify / stamp
  - round-trip at the same version (no behavior change)
  - TCP request from a different major → version_mismatch error + agent raises
  - UDP (Kademlia) foreign-major message dropped, no reply, no crash
  - legacy versionless message accepted with a one-time deprecation warning
  - unknown extra field in a v1.x message ignored
  - probe_peer path stamped + checked
  - foreign-major record ingested (not dropped), with a debug log
  - renew loop stops (no retry) on version mismatch
  - bind_response carries its type field

Run:  python3 -m unittest tests.test_protocol -v
"""

import logging
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a import PROTOCOL_VERSION, ProtocolVersionError
from d2a.protocol import (
    major_of, versions_compatible, classify, stamp, VERSION_FIELD, _warned_legacy,
)
from d2a.swarm import LANSwarm
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent, LeaseLostError

NEXT_MAJOR = "2.0"   # a deliberately incompatible major


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _install_version_bumper(swarm, version, only_type=None):
    """
    Replace a swarm's _tcp_send with a raw sender that forces `version` on the
    wire, bypassing the production stamp (which lives inside _tcp_send and would
    otherwise rewrite v back to the real PROTOCOL_VERSION). Returns a counter dict
    {"count": n} of bumped messages. If only_type is given, only that message type
    is bumped; others go out stamped normally.
    """
    import json
    counter = {"count": 0}

    def _send(target_node_id, message, recv=False, timeout=5.0):
        with swarm._lock:
            addr = swarm._peers.get(target_node_id)
        if not addr:
            return None if recv else False
        message = dict(message)
        if only_type is None or message.get("type") == only_type:
            counter["count"] += 1
            message[VERSION_FIELD] = version              # forced, not re-stamped
        else:
            message.setdefault(VERSION_FIELD, PROTOCOL_VERSION)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(addr)
            s.sendall((json.dumps(message, default=str) + "\n").encode())
            if not recv:
                s.close()
                return True
            data = b""
            while b"\n" not in data:
                c = s.recv(65535)
                if not c:
                    break
                data += c
            s.close()
            line = data.split(b"\n")[0].strip()
            return json.loads(line.decode()) if line else None
        except Exception:
            return None if recv else False

    swarm._tcp_send = _send
    return counter


# ── pure helpers ─────────────────────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):
    def test_major_of(self):
        self.assertEqual(major_of("1.0"), 1)
        self.assertEqual(major_of("1.9"), 1)
        self.assertEqual(major_of("2.3"), 2)
        self.assertEqual(major_of(None), 0)      # legacy
        self.assertEqual(major_of(""), 0)
        self.assertEqual(major_of("garbage"), -1)

    def test_compatible_same_major_only(self):
        self.assertTrue(versions_compatible("1.0", "1.7"))    # minor differs → ok
        self.assertFalse(versions_compatible("1.0", "2.0"))
        self.assertFalse(versions_compatible("garbage", "1.0"))

    def test_classify(self):
        self.assertEqual(classify(PROTOCOL_VERSION), "current")
        self.assertEqual(classify("1.5"), "current")          # additive minor
        self.assertEqual(classify(None), "legacy")
        self.assertEqual(classify(NEXT_MAJOR), "incompatible")

    def test_stamp_top_level_no_envelope(self):
        m = stamp({"type": "bind_request", "x": 1})
        self.assertEqual(m[VERSION_FIELD], PROTOCOL_VERSION)
        self.assertEqual(m["type"], "bind_request")           # payload untouched
        self.assertEqual(m["x"], 1)


# ── low-level TCP: mismatch / legacy / unknown-field, transport-agnostic core ────

class TestTCPVersionGate(unittest.TestCase):
    """Drives LANSwarm's TCP core directly (DHTSwarm reuses it verbatim)."""

    def setUp(self):
        self.captured = []
        self.server = LANSwarm(node_id="srv")
        # server never uses UDP broadcast here; start only the TCP loop
        self.server._running = True
        import threading
        threading.Thread(target=self.server._tcp_loop, daemon=True).start()
        self.server.message_handler = self._handler
        self.addr = self.server.address

    def tearDown(self):
        self.server.stop()

    def _handler(self, msg):
        self.captured.append(msg)
        return {"type": "ok", "echo": msg.get("type")}

    def _raw_request(self, obj: dict) -> dict:
        """Send a raw JSON line (bypassing stamp) and read one JSON line back."""
        import json
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(self.addr)
        s.sendall((json.dumps(obj) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65535)
            if not chunk:
                break
            data += chunk
        s.close()
        line = data.split(b"\n")[0].strip()
        return json.loads(line.decode()) if line else None

    def test_same_version_roundtrip(self):
        resp = self._raw_request({"type": "ping", "v": PROTOCOL_VERSION})
        self.assertEqual(resp.get("type"), "ok")
        self.assertEqual(resp.get(VERSION_FIELD), PROTOCOL_VERSION)   # response stamped
        self.assertEqual(len(self.captured), 1, "handler ran")

    def test_major_mismatch_rejected(self):
        resp = self._raw_request({"type": "ping", "v": NEXT_MAJOR})
        self.assertEqual(resp.get("type"), "error")
        self.assertEqual(resp.get("reason"), "version_mismatch")
        self.assertEqual(resp.get("peer_version"), PROTOCOL_VERSION)
        self.assertEqual(len(self.captured), 0, "handler NOT run on mismatch")

    def test_legacy_accepted_with_warning(self):
        _warned_legacy.clear()
        with self.assertLogs("d2a.protocol", level="WARNING") as cm:
            resp = self._raw_request({"type": "ping"})               # no v field
        self.assertEqual(resp.get("type"), "ok", "legacy accepted")
        self.assertTrue(any("legacy versionless" in m for m in cm.output))
        self.assertEqual(len(self.captured), 1, "handler ran for legacy")

    def test_unknown_field_ignored(self):
        resp = self._raw_request({
            "type": "ping", "v": "1.4", "brand_new_field": {"nested": True},
        })
        self.assertEqual(resp.get("type"), "ok", "future-minor + unknown field ok")
        self.assertEqual(self.captured[-1]["brand_new_field"], {"nested": True},
                         "unknown field passed through untouched, not rejected")


# ── Kademlia UDP: foreign-major dropped, no reply, no crash ──────────────────────

class TestUDPVersionGate(unittest.TestCase):
    def setUp(self):
        self.nodes = []

    def tearDown(self):
        for n in self.nodes:
            n.stop()
        time.sleep(0.05)

    def _spawn(self, ttl=30):
        n = KademliaNode(node_id=f"n{len(self.nodes)}", udp_port=free_udp_port(), ttl=ttl)
        n.start()
        self.nodes.append(n)
        return n

    def test_foreign_major_udp_dropped_no_reply(self):
        import json
        node = self._spawn()
        # A PING would normally get a PONG. Foreign-major PING must be dropped silently.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(1.0)
        probe.bind(("", 0))
        ping = {"type": "PING", "sender_id": "probe",
                "sender_ip": "127.0.0.1", "sender_port": probe.getsockname()[1],
                "v": NEXT_MAJOR}
        probe.sendto(json.dumps(ping).encode(), ("127.0.0.1", node.udp_port))
        with self.assertRaises(socket.timeout):
            probe.recvfrom(65535)                       # no PONG comes back
        probe.close()
        self.assertTrue(node._running, "node survived foreign-major packet")

    def test_same_major_udp_gets_reply(self):
        import json
        node = self._spawn()
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(2.0)
        probe.bind(("", 0))
        ping = {"type": "PING", "sender_id": "probe",
                "sender_ip": "127.0.0.1", "sender_port": probe.getsockname()[1],
                "v": PROTOCOL_VERSION}
        probe.sendto(json.dumps(ping).encode(), ("127.0.0.1", node.udp_port))
        data, _ = probe.recvfrom(65535)                 # PONG expected
        reply = json.loads(data.decode())
        self.assertEqual(reply.get("type"), "PONG")
        self.assertEqual(reply.get(VERSION_FIELD), PROTOCOL_VERSION)
        probe.close()

    def test_foreign_major_record_ingested_with_debug_log(self):
        node = self._spawn()
        rec = {"node_id": "authorX", "name": "compute",
               "address": ["127.0.0.1", 9], "ts": time.time(), "v": NEXT_MAJOR}
        with self.assertLogs("d2a.protocol", level="DEBUG") as cm:
            node._merge_record("cap:compute", rec)      # relay ingest path
        # ingested despite foreign major (record-level v is the eventual gate)
        live = node._live_records("cap:compute")
        self.assertEqual([r["node_id"] for r in live], ["authorX"])
        self.assertTrue(any("foreign-major record" in m for m in cm.output))


# ── end-to-end over both transports ──────────────────────────────────────────────

class ProtoE2EMixin:
    def setUp(self):
        _warned_legacy.clear()
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

    def bind(self, agent, device, cap, priority=5):
        self._discover(agent, device, cap)
        return agent.bind_remote_to(device.node_id, cap, priority)

    # round-trip at the same version — full bind + data pull works unchanged
    def test_same_version_bind_and_read(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("ag")
        r = self.bind(a, d, "sensing")
        self.assertEqual(r.get("type"), "bind_response", "bind reply is now typed")
        self.assertTrue(r.get("verified"))
        self.assertEqual(r.get(VERSION_FIELD), PROTOCOL_VERSION, "response stamped")
        reading = a.request_data(r, "sensing")
        self.assertEqual(reading.get("type"), "reading")

    # agent that speaks a different major → device rejects, agent raises typed error
    def test_agent_major_mismatch_raises(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("ag")
        self._discover(a, d, "sensing")
        # Force a different major ON THE WIRE. Must bypass the production stamp
        # (which lives inside _tcp_send and would rewrite v back to 1.0), so we
        # replace _tcp_send with a raw sender that stamps NEXT_MAJOR itself.
        _install_version_bumper(a.swarm, NEXT_MAJOR)

        with self.assertRaises(ProtocolVersionError) as ctx:
            a.bind_remote_to(d.node_id, "sensing")
        self.assertEqual(ctx.exception.local_version, PROTOCOL_VERSION)
        self.assertEqual(ctx.exception.peer_version, PROTOCOL_VERSION)  # device's version

    # renew loop stops (does NOT retry) on version mismatch → lease lost
    def test_renew_loop_stops_on_version_mismatch(self):
        d = self.make_device("dev", ["compute", "sensing"], lease_ttl=2)
        a = self.make_agent("ag", auto_renew=True)
        r = self.bind(a, d, "sensing")
        self.assertTrue(r.get("verified"))

        # After the bind, poison only renew_binding messages with a bad major.
        calls = _install_version_bumper(a.swarm, NEXT_MAJOR, only_type="renew_binding")

        # Wait past one renew (~half of TTL=2 → ~1s) plus margin.
        deadline = time.time() + 6
        while time.time() < deadline:
            with a._leases_lock:
                lease = a._leases.get(r["binding_id"])
                lost = lease.get("lost") if lease else "gone"
            if lost:
                break
            time.sleep(0.1)
        self.assertEqual(lost, "version_mismatch", "renew mismatch marks lease lost")
        self.assertEqual(calls["count"], 1, "stopped after ONE renew — no retry storm")
        # The lease is now gone; using the binding surfaces LeaseLostError (whose
        # reason names the version mismatch) — never a silent failure.
        with self.assertRaises(LeaseLostError) as ctx:
            a.request_data(r, "sensing")
        self.assertEqual(ctx.exception.reason, "version_mismatch")


class TestProtoLAN(ProtoE2EMixin, unittest.TestCase):
    def _setup_transport(self): pass
    def _teardown_transport(self): pass

    def make_device(self, name, caps, lease_ttl=300):
        d = DeviceRuntime(name=name, capability_override=caps, lease_ttl=lease_ttl)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name, auto_renew=False):
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
                    "node_id": device.node_id, "name": c.name, "tags": list(c.tags),
                    "live_state": dict(c.live_state), "public_key": device.public_key,
                    "address": [ip, port], "device_class": device.device_class, "ts": now,
                }
        agent.swarm.add_known_peer(device.node_id, ip, port)


class TestProtoDHT(ProtoE2EMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="proto-boot", udp_port=free_udp_port(), ttl=30)
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

    def make_device(self, name, caps, lease_ttl=300):
        d = DeviceRuntime(name=name, capability_override=caps, lease_ttl=lease_ttl)
        self._attach(d)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)
        return d

    def make_agent(self, name, auto_renew=False):
        a = RemoteAgent(name=name, auto_renew=auto_renew)
        self._attach(a)
        a.start()
        self.agents.append(a)
        time.sleep(0.3)
        return a

    def _discover(self, agent, device, cap):
        agent.find_capability(cap)


# ── probe_peer bypass path: stamped + checked ────────────────────────────────────

class TestProbePeerVersioned(unittest.TestCase):
    def setUp(self):
        self.device = DeviceRuntime(name="probe-dev", capability_override=["compute", "sensing"])
        self.device.start_swarm()

    def tearDown(self):
        self.device.stop_swarm()

    def test_probe_peer_same_version_gets_records(self):
        client = LANSwarm(node_id="probe-client")
        ip, port = self.device.swarm.address
        records = client.probe_peer(ip, port)
        self.assertTrue(records, "probe_peer returned capability records")
        self.assertTrue(any(r["name"] == "sensing" for r in records))

    def test_probe_peer_stamps_request(self):
        # The device's handler should never see a versionless capabilities_request
        # (probe_peer stamps it), so no legacy warning fires for the probe.
        _warned_legacy.clear()
        client = LANSwarm(node_id="probe-client2")
        ip, port = self.device.swarm.address
        logging.getLogger("d2a.protocol").setLevel(logging.WARNING)
        client.probe_peer(ip, port)
        self.assertNotIn("tcp:probe-client2", _warned_legacy,
                         "probe_peer request was stamped, not treated as legacy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
