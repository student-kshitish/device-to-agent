"""
tests/test_leases.py — DHCP-style binding leases, transport-agnostic.

The lease logic lives ABOVE the transport, so every wire-level test runs on both
LANSwarm and DHTSwarm via two concrete subclasses of LeaseTestsMixin. Device clock
is the sole authority for expiry — no test ever compares an agent clock to a device
clock.

Covers:
  - lease TTL present in bind response
  - renewal extends expiry
  - agent death → device frees the slot; a second agent binds after expiry
  - expiry fires the waitqueue auto-grant (queued agent gets slot, no release)
  - subscription torn down on expiry (no frames after, even across sweeps)
  - renew of unknown/expired binding denied
  - auto-renew keeps a long binding alive
  - renew retry survives one simulated send failure
  - preemption and expiry leave identical broker invariants (shared path)

Run:  python3 -m unittest tests.test_leases -v
"""

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a.broker import CapabilityBroker
from d2a.swarm_dht import DHTSwarm
from d2a.kademlia import KademliaNode
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent, LeaseLostError

TTL = 2   # short lease for fast tests; sweeper interval = min(TTL/10, 5) = 0.2s


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── (b) pure-broker: preemption and expiry share one teardown path ───────────────

class _FakeRuntime:
    node_id = "dev-node"
    private_key = "k" * 32
    lease_ttl = TTL
    def get_capability(self, name):
        return True if name == "x" else None


class TestSharedTeardownInvariants(unittest.TestCase):
    """Preemption and expiry must leave the broker with identical invariants,
    because both go through _remove_active_bind()."""

    def _assert_torn_down(self, broker, binding_id, reason):
        b = broker.get_binding(binding_id)
        self.assertIsNotNone(b)
        self.assertEqual(b.status, reason)
        self.assertEqual(b.release_reason, reason)
        # not present in ANY active slot list
        in_active = any(
            ab.binding_id == binding_id
            for binds in broker.active_binds.values() for ab in binds
        )
        self.assertFalse(in_active, f"{reason} binding must not hold a slot")

    def test_preemption_and_expiry_identical_invariants(self):
        broker = CapabilityBroker(_FakeRuntime())
        broker.quotas = {"x": 1}

        # A granted, then B preempts A (B higher priority = lower number)
        rA = broker.request_bind("agentA", "x", [], priority=5)
        bidA = rA["binding_id"]
        rB = broker.request_bind("agentB", "x", [], priority=1)
        bidB = rB["binding_id"]
        self.assertEqual(rB["status"], "granted_by_preemption")

        # A left the active set via the shared path, marked "preempted"
        self._assert_torn_down(broker, bidA, "preempted")

        # Now expire B via the shared path (force its token into the past)
        broker.get_binding(bidB).token = broker.get_binding(bidB).token.__class__(
            **{**broker.get_binding(bidB).token.__dict__, "expires_at": time.time() - 1}
        )
        expired = broker.sweep_expired()
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["binding_id"], bidB)

        # B torn down with identical invariants, only the reason differs
        self._assert_torn_down(broker, bidB, "expired")

        # And expiry fired the waitqueue auto-grant: A (re-queued on preemption)
        # now holds the slot again, with NO explicit release anywhere.
        active_agents = [ab.agent_id for binds in broker.active_binds.values() for ab in binds]
        self.assertEqual(active_agents, ["agentA"])


# ── transport-parametrized wire-level lease tests ────────────────────────────────

class LeaseTestsMixin:
    """Shared lease tests. Subclasses provide make_device/make_agent/_discover."""

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

    def renew_over_wire(self, agent, binding) -> dict:
        return agent.swarm.send_and_recv(binding["provider_node_id"], {
            "type": "renew_binding", "from_node": agent.agent_id,
            "binding_id": binding["binding_id"], "capability_name": binding["capability_name"],
        }, timeout=5.0)

    # 1 — TTL present in bind response
    def test_bind_response_carries_lease(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("ag", auto_renew=False)
        r = self.bind(a, d, "sensing")
        self.assertTrue(r.get("verified"))
        self.assertEqual(r.get("lease_ttl"), TTL)
        self.assertGreater(r.get("lease_expires_at", 0), time.time())
        self.assertLessEqual(r.get("lease_expires_at", 0), time.time() + TTL + 1)

    # 2 — renewal extends expiry
    def test_renewal_extends_expiry(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("ag", auto_renew=False)
        r = self.bind(a, d, "sensing")
        first_exp = r["lease_expires_at"]
        time.sleep(0.5)
        resp = self.renew_over_wire(a, r)
        self.assertEqual(resp.get("status"), "renewed")
        self.assertGreater(resp.get("lease_expires_at"), first_exp)

    # 3 — agent death frees the slot; a second agent binds after expiry
    def test_agent_death_frees_slot(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a1 = self.make_agent("dead", auto_renew=False)   # never renews == dead
        r1 = self.bind(a1, d, "sensing")
        self.assertTrue(r1.get("verified"))
        # quota=1: a fresh agent cannot bind yet
        a2 = self.make_agent("live", auto_renew=False)
        r2_early = self.bind(a2, d, "sensing")
        self.assertEqual(r2_early.get("status"), "queued")

        time.sleep(TTL + 0.8)                            # let the lease lapse + sweep
        r2 = self._rebind(a2, d, "sensing")
        # after expiry the slot is free (a2 may have been auto-granted from the queue,
        # or binds fresh) — either way a2 now holds an active sensing slot on d
        active = [ab.agent_id for binds in d.broker.active_binds.values() for ab in binds]
        self.assertIn(a2.agent_id, active, "second agent holds the freed slot")

    # (a) expiry fires the waitqueue auto-grant with no explicit release
    def test_expiry_autogrants_waitqueue(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a1 = self.make_agent("holder", auto_renew=False)
        r1 = self.bind(a1, d, "sensing")
        self.assertTrue(r1.get("verified"))
        a2 = self.make_agent("waiter", auto_renew=False)
        r2 = self.bind(a2, d, "sensing", priority=5)
        self.assertEqual(r2.get("status"), "queued")

        time.sleep(TTL + 0.8)                            # a1 lapses; sweeper auto-grants a2
        active = [ab.agent_id for binds in d.broker.active_binds.values() for ab in binds]
        self.assertEqual(active, [a2.agent_id],
                         "queued agent auto-granted on expiry, no release called")

    # 4 — subscription torn down on expiry; no frames after teardown
    def test_subscription_torn_down_on_expiry(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("streamer", auto_renew=False)
        r = self.bind(a, d, "sensing")
        frames = []
        a.start_stream(r, lambda f: frames.append(time.time()), hz=10.0)
        time.sleep(0.6)
        self.assertGreater(len(frames), 0, "frames flow while lease is live")

        time.sleep(TTL + 0.8)                            # lease expires, sweeper unsubscribes
        count_at_expiry = len(frames)
        time.sleep(1.2)                                  # window across multiple sweeper ticks
        self.assertEqual(len(frames), count_at_expiry,
                         "no frames delivered after lease expiry")

    # 5 — renew of unknown / expired binding denied
    def test_renew_denied_unknown_and_expired(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("ag", auto_renew=False)
        r = self.bind(a, d, "sensing")

        bogus = dict(r); bogus["binding_id"] = "does-not-exist"
        resp = self.renew_over_wire(a, bogus)
        self.assertEqual(resp.get("status"), "denied")
        self.assertEqual(resp.get("reason"), "unknown_binding")

        time.sleep(TTL + 0.8)                            # let the real lease expire
        resp2 = self.renew_over_wire(a, r)
        self.assertEqual(resp2.get("status"), "denied")
        self.assertEqual(resp2.get("reason"), "expired")

    # 6 — auto-renew keeps a long binding alive
    def test_autorenew_keeps_alive(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("ag", auto_renew=True)
        r = self.bind(a, d, "sensing")
        time.sleep(TTL * 2.5)                            # well past a single TTL
        reading = a.request_data(r, "sensing")          # must not raise LeaseLostError
        self.assertEqual(reading.get("type"), "reading")

    # (c) renew retry survives one simulated send failure
    def test_renew_survives_one_send_failure(self):
        d = self.make_device("dev", ["compute", "sensing"])
        a = self.make_agent("ag", auto_renew=True)

        orig = a.swarm.send_and_recv
        state = {"dropped": 0}
        def flaky(target, msg, timeout=5.0):
            if msg.get("type") == "renew_binding" and state["dropped"] == 0:
                state["dropped"] += 1
                return None                              # simulate one dropped renew
            return orig(target, msg, timeout=timeout)
        a.swarm.send_and_recv = flaky

        r = self.bind(a, d, "sensing")
        time.sleep(TTL * 2.5)
        self.assertEqual(state["dropped"], 1, "exactly one renew was dropped")
        reading = a.request_data(r, "sensing")          # binding survived the drop
        self.assertEqual(reading.get("type"), "reading")

    def _rebind(self, agent, device, cap):
        """Fresh bind attempt after expiry (rediscover in case DHT record moved)."""
        return self.bind(agent, device, cap)


# ── LAN concrete ─────────────────────────────────────────────────────────────────

class TestLeasesLAN(LeaseTestsMixin, unittest.TestCase):
    def _setup_transport(self):
        pass

    def _teardown_transport(self):
        pass

    def make_device(self, name, caps):
        d = DeviceRuntime(name=name, capability_override=caps, lease_ttl=TTL)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name, auto_renew=True):
        a = RemoteAgent(name=name, auto_renew=auto_renew)
        a.start()
        self.agents.append(a)
        return a

    def _discover(self, agent, device, cap):
        # Seed like a UDP announce would (deterministic on loopback).
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

class TestLeasesDHT(LeaseTestsMixin, unittest.TestCase):
    def _setup_transport(self):
        self.boot = KademliaNode(node_id="lease-bootstrap", udp_port=free_udp_port(), ttl=30)
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
        d = DeviceRuntime(name=name, capability_override=caps, lease_ttl=TTL)
        self._attach_dht(d)
        d.start_swarm()
        self.devices.append(d)
        time.sleep(0.4)                                 # settle DHT publish
        return d

    def make_agent(self, name, auto_renew=True):
        a = RemoteAgent(name=name, auto_renew=auto_renew)
        self._attach_dht(a)
        a.start()
        self.agents.append(a)
        time.sleep(0.3)
        return a

    def _discover(self, agent, device, cap):
        agent.find_capability(cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
