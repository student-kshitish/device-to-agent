"""
tests/test_departure.py — graceful device departure (v1.4 Part 2).

The graceful path is strictly ADDITIVE on top of the existing lease machinery:

  * `DeviceRuntime.stop_swarm()` (and `stop()` / context-manager exit) now, before
    tearing down the transport: tears every active binding down through the ONE
    unified path (reason "shutdown"), pushes a `device_shutdown` notice to each
    bound agent, and unpublishes its records so discovery drops it immediately.
  * The agent surfaces `device_shutdown` DISTINCTLY from a lapsed lease —
    `LeaseLostError.code == errors.DEVICE_SHUTDOWN` vs `errors.LEASE_EXPIRED`.
  * An UNGRACEFUL kill (transport stopped without the graceful call, i.e. a crashed
    process) is unchanged: no notice, no unpublish — peers TTL-age the record and
    renew fails exactly as before.

Both transports (LAN + DHT) are exercised via the mixin, mirroring test_leases.
"""

import socket
import time
import unittest

from d2a import errors
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


class DepartureMixin:
    """Shared graceful/ungraceful departure tests. Subclasses provide
    make_device / make_agent / _discover / _setup_transport (like test_leases)."""

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

    def bind(self, agent, device, cap, priority=5):
        self._discover(agent, device, cap)
        return agent.bind_remote_to(device.node_id, cap, priority)

    def _device_records(self, agent, cap, device):
        return [r for r in agent.find_capability(cap)
                if r.get("node_id") == device.node_id]

    # 1 — a bound agent receives the shutdown notice, distinct from lease_expired
    def test_bound_agent_receives_shutdown_notice(self):
        d = self.make_device("leaver", ["compute", "sensing"])
        a = self.make_agent("watcher", auto_renew=False)
        r = self.bind(a, d, "sensing")
        self.assertTrue(r.get("verified"), f"bind not verified: {r}")
        bid = r["binding_id"]

        d.stop_swarm()                                    # graceful departure
        # the device_shutdown push is best-effort async — give it a moment
        deadline = time.time() + 3
        while time.time() < deadline and a._leases.get(bid, {}).get("lost") is None:
            time.sleep(0.05)

        # lease marked lost with the DISTINCT code (not lease_expired)
        self.assertEqual(a._leases.get(bid, {}).get("lost"), errors.DEVICE_SHUTDOWN)
        with self.assertRaises(LeaseLostError) as ctx:
            a.request_data(r, "sensing")
        self.assertEqual(ctx.exception.code, errors.DEVICE_SHUTDOWN)
        self.assertNotEqual(ctx.exception.code, errors.LEASE_EXPIRED)

    # 2 — records vanish from discover() IMMEDIATELY (no full-TTL ghost)
    def test_records_dropped_immediately_on_stop(self):
        # A long record TTL so that if the record were merely TTL-aging it would
        # STILL be present now — any absence must be the graceful unpublish.
        d = self.make_device("here-then-gone", ["compute", "sensing"], ttl=30)
        a = self.make_agent("seeker", auto_renew=False, ttl=30)
        self.bind(a, d, "sensing")
        self.assertTrue(self._device_records(a, "sensing", d), "present while up")

        d.stop_swarm()
        time.sleep(self.PROPAGATE)                        # withdraw / tombstone settle, << TTL
        self.assertEqual(self._device_records(a, "sensing", d), [],
                         "graceful departure drops the record now, not after TTL")

    # 3 — every binding is torn down through the unified path, reason "shutdown"
    def test_teardown_reason_recorded(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("ag", auto_renew=False)
        r = self.bind(a, d, "sensing")
        bid = r["binding_id"]

        d.stop_swarm()
        b = d.broker.get_binding(bid)
        self.assertIsNotNone(b)
        self.assertEqual(b.status, "shutdown")
        self.assertEqual(b.release_reason, "shutdown")
        # and the slot is gone from the active set
        in_active = any(ab.binding_id == bid
                        for binds in d.broker.active_binds.values() for ab in binds)
        self.assertFalse(in_active)

    # 4 — ungraceful kill is UNCHANGED: no notice, record ghosts (TTL-ages), additive
    def test_ungraceful_kill_is_additive(self):
        d = self.make_device("crasher", ["compute", "sensing"], ttl=30)
        a = self.make_agent("survivor", auto_renew=False, ttl=30)
        r = self.bind(a, d, "sensing")
        bid = r["binding_id"]

        # Kill the transport WITHOUT the graceful call — like a crashed process.
        d.swarm.stop()
        d._sweeper_running = False
        time.sleep(self.PROPAGATE)

        # no device_shutdown was sent → the lease is NOT marked lost yet
        self.assertIsNone(a._leases.get(bid, {}).get("lost"),
                          "silent death sends no notice")
        # and the record still ghosts in discovery (no unpublish happened)
        self.assertTrue(self._device_records(a, "sensing", d),
                        "ungraceful death leaves a TTL ghost, exactly as before")

    # 5 — ungraceful kill with auto-renew: loss surfaces as renew failure / TTL,
    #     NEVER as device_shutdown
    def test_ungraceful_renew_failure_not_shutdown(self):
        d = self.make_device("dev", ["compute", "sensing"], lease_ttl=2)
        a = self.make_agent("ag", auto_renew=True)
        r = self.bind(a, d, "sensing")
        bid = r["binding_id"]

        d.swarm.stop()                                    # crash; renews will fail
        d._sweeper_running = False

        deadline = time.time() + 6
        while time.time() < deadline and a._leases.get(bid, {}).get("lost") is None:
            time.sleep(0.1)
        lost = a._leases.get(bid, {}).get("lost")
        self.assertEqual(lost, errors.LEASE_EXPIRED,
                         "renew failure ages out as lease_expired, not device_shutdown")


# ── LAN concrete ─────────────────────────────────────────────────────────────────

class TestDepartureLAN(DepartureMixin, unittest.TestCase):
    PROPAGATE = 0.4        # UDP withdraw broadcast round-trip

    def _setup_transport(self):
        pass

    def _teardown_transport(self):
        pass

    def make_device(self, name, caps, ttl=30, lease_ttl=300):
        # LAN record TTL is a swarm module constant (30s); `ttl` is accepted for a
        # uniform signature with the DHT concrete and is not per-instance here.
        d = DeviceRuntime(name=name, capability_override=caps, lease_ttl=lease_ttl)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name, auto_renew=True, ttl=30):
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


# ── DHT concrete ─────────────────────────────────────────────────────────────────

class TestDepartureDHT(DepartureMixin, unittest.TestCase):
    PROPAGATE = 0.6        # tombstone STORE fan-out to the K closest

    def _setup_transport(self):
        self.boot = KademliaNode(node_id="departure-boot", udp_port=free_udp_port(), ttl=30)
        self.boot.start()
        self.boot_addr = ("127.0.0.1", self.boot.udp_port)

    def _teardown_transport(self):
        self.boot.stop()

    def _attach_dht(self, obj, ttl):
        node_id = getattr(obj, "node_id", None) or obj.agent_id
        try: obj.swarm._tcp_srv.close()
        except Exception: pass
        obj.swarm = DHTSwarm(node_id=node_id, dht_port=free_udp_port(),
                             bootstrap=self.boot_addr, ttl=ttl)

    def make_device(self, name, caps, ttl=30, lease_ttl=300):
        d = DeviceRuntime(name=name, capability_override=caps, lease_ttl=lease_ttl)
        self._attach_dht(d, ttl)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)
        return d

    def make_agent(self, name, auto_renew=True, ttl=30):
        a = RemoteAgent(name=name, auto_renew=auto_renew)
        self._attach_dht(a, ttl)
        a.start()
        self.agents.append(a)
        time.sleep(0.3)
        return a

    def _discover(self, agent, device, cap):
        agent.find_capability(cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
