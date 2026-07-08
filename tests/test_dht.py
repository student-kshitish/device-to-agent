"""
tests/test_dht.py — Kademlia DHT discovery + DHTSwarm end-to-end.

Pure stdlib unittest. Covers plan item 7:
  1. Routing table  — XOR ordering, K-bucket LRU eviction, stale eviction.
  2. STORE/FIND_VALUE — multi-provider merge + TTL expiry.
  3. Bootstrap      — a new node joins via one seed and learns the mesh.
  4. End-to-end     — bind over DHT with NO prior knowledge of device address.
  5. Node departure — records expire after a device stops.

Run:  python3 -m unittest tests.test_dht -v
      (from repo root)
"""

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from d2a.kademlia import (
    KademliaNode, RoutingTable, KBucket, K, hash_id, xor_distance,
)
from d2a.swarm_dht import DHTSwarm
from runtimes.device_runtime import DeviceRuntime
from agents.remote_agent import RemoteAgent
from tests._env import use_tmp_home, restore_home


def setUpModule():
    # Isolate persisted Ed25519 keys + TOFU pins to a tmpdir (never touch ~/.d2a).
    use_tmp_home()


def tearDownModule():
    restore_home()


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def attach_dht(obj, dht_port: int, bootstrap, ttl: int = 30) -> DHTSwarm:
    """
    Swap a DeviceRuntime/RemoteAgent's default LAN transport for a DHTSwarm bound
    to the object's own node_id. Closes the idle default TCP socket first.
    """
    node_id = getattr(obj, "node_id", None) or obj.agent_id
    try:
        obj.swarm._tcp_srv.close()
    except Exception:
        pass
    dht = DHTSwarm(node_id=node_id, dht_port=dht_port, bootstrap=bootstrap, ttl=ttl)
    obj.swarm = dht
    return dht


# ── 1. Routing table ─────────────────────────────────────────────────────────────

class TestRoutingTable(unittest.TestCase):

    def test_xor_distance_ordering(self):
        own = hash_id("owner")
        rt = RoutingTable(own)
        ids = [hash_id(f"peer{i}") for i in range(10)]
        for i, rid in enumerate(ids):
            rt.add_node(rid, "127.0.0.1", 6000 + i)
        target = ids[3]
        closest = rt.find_closest(target, 5)
        dists = [xor_distance(n[0], target) for n in closest]
        self.assertEqual(dists, sorted(dists), "find_closest must be XOR-sorted")
        self.assertEqual(closest[0][0], target, "exact target is its own closest")

    def test_own_id_never_inserted(self):
        own = hash_id("me")
        rt = RoutingTable(own)
        rt.add_node(own, "127.0.0.1", 1)
        self.assertEqual(rt.size(), 0)

    def test_kbucket_lru_eviction(self):
        b = KBucket()
        rids = [hash_id(f"n{i}") for i in range(K + 3)]
        for i, rid in enumerate(rids):
            b.add(rid, "127.0.0.1", 7000 + i)
        self.assertEqual(len(b.nodes), K, "bucket capped at K")
        present = {n[0] for n in b.nodes}
        # first 3 (oldest) evicted, last K present
        self.assertNotIn(rids[0], present)
        self.assertNotIn(rids[2], present)
        self.assertIn(rids[-1], present)

    def test_kbucket_refresh_moves_to_tail(self):
        b = KBucket()
        rids = [hash_id(f"m{i}") for i in range(K)]
        for i, rid in enumerate(rids):
            b.add(rid, "127.0.0.1", 7100 + i)
        # refresh the oldest → it should survive the next eviction
        b.add(rids[0], "127.0.0.1", 7100)
        b.add(hash_id("new-one"), "127.0.0.1", 7999)   # forces one eviction
        present = {n[0] for n in b.nodes}
        self.assertIn(rids[0], present, "refreshed node moved to tail, not evicted")
        self.assertNotIn(rids[1], present, "the now-oldest was evicted instead")

    def test_evict_stale(self):
        rt = RoutingTable(hash_id("owner2"))
        for i in range(5):
            rt.add_node(hash_id(f"s{i}"), "127.0.0.1", 8000 + i)
        self.assertEqual(rt.size(), 5)
        time.sleep(0.05)
        rt.evict_stale(0.01)                            # everything older than 10ms
        self.assertEqual(rt.size(), 0)


# ── DHT mesh test base ───────────────────────────────────────────────────────────

class DHTMeshBase(unittest.TestCase):
    def setUp(self):
        self.nodes: list[KademliaNode] = []

    def tearDown(self):
        for n in self.nodes:
            n.stop()
        time.sleep(0.05)

    def spawn(self, ttl=30, bootstrap=None, verbose=False) -> KademliaNode:
        n = KademliaNode(node_id=hash_id(f"node-{len(self.nodes)}-{time.time()}")[:16],
                         udp_port=free_udp_port(), ttl=ttl, verbose=verbose)
        n.start(bootstrap=bootstrap)
        self.nodes.append(n)
        return n

    @staticmethod
    def boot_addr(node: KademliaNode) -> tuple[str, int]:
        return ("127.0.0.1", node.udp_port)


# ── 2. STORE / FIND_VALUE: multi-provider merge + TTL ────────────────────────────

class TestStoreFindValue(DHTMeshBase):

    def test_multi_provider_merge(self):
        boot = self.spawn()
        b = self.spawn(bootstrap=self.boot_addr(boot))
        c = self.spawn(bootstrap=self.boot_addr(boot))
        time.sleep(0.5)                                 # warm routing tables

        b.store("cap:sense", {"node_id": "providerB", "name": "sense",
                              "address": ["127.0.0.1", 11111], "ts": time.time()})
        c.store("cap:sense", {"node_id": "providerC", "name": "sense",
                              "address": ["127.0.0.1", 22222], "ts": time.time()})
        time.sleep(0.3)

        found = boot.find_value("cap:sense", timeout=2.0)
        providers = {r["node_id"] for r in found}
        self.assertEqual(providers, {"providerB", "providerC"},
                         "both providers merged under one key")

    def test_ttl_expiry(self):
        boot = self.spawn(ttl=2)
        b = self.spawn(bootstrap=self.boot_addr(boot), ttl=2)
        time.sleep(0.4)

        b.store("cap:x", {"node_id": "pB", "name": "x",
                          "address": ["127.0.0.1", 1], "ts": time.time()})
        time.sleep(0.3)
        self.assertTrue(boot.find_value("cap:x", timeout=1.0), "present before TTL")

        b.stop()                                        # provider stops re-storing
        self.nodes.remove(b)
        time.sleep(2.6)                                 # exceed ttl=2
        self.assertEqual(boot.find_value("cap:x", timeout=1.0), [],
                         "record pruned after TTL once provider departs")


# ── 3. Bootstrap ─────────────────────────────────────────────────────────────────

class TestBootstrap(DHTMeshBase):

    def test_join_and_transitive_discovery(self):
        boot = self.spawn()
        b = self.spawn(bootstrap=self.boot_addr(boot))
        time.sleep(0.4)
        self.assertGreaterEqual(boot.routing_table.size(), 1, "seed learned joiner")
        self.assertGreaterEqual(b.routing_table.size(), 1, "joiner learned seed")

        # c joins via boot only; should transitively learn b through FOUND_NODES
        c = self.spawn(bootstrap=self.boot_addr(boot))
        time.sleep(0.6)
        c_ids = {n[0] for n in c.routing_table.all_nodes()}
        self.assertIn(b.routing_id, c_ids,
                      "c discovered b transitively via the seed's FOUND_NODES walk")


# ── DHTSwarm (device/agent) test base ────────────────────────────────────────────

class DHTSwarmBase(unittest.TestCase):
    def setUp(self):
        self.boot = KademliaNode(node_id="bootstrap-node", udp_port=free_udp_port(), ttl=self.TTL)
        self.boot.start()
        self.boot_addr = ("127.0.0.1", self.boot.udp_port)
        self.devices: list[DeviceRuntime] = []
        self.agents: list[RemoteAgent] = []

    TTL = 30

    def tearDown(self):
        for d in self.devices:
            d.stop_swarm()
        for a in self.agents:
            a.stop()
        self.boot.stop()
        time.sleep(0.05)

    def make_device(self, name, caps) -> DeviceRuntime:
        d = DeviceRuntime(name=name, capability_override=caps)
        attach_dht(d, free_udp_port(), self.boot_addr, ttl=self.TTL)
        d.start_swarm()
        self.devices.append(d)
        return d

    def make_agent(self, name) -> RemoteAgent:
        a = RemoteAgent(name=name)
        attach_dht(a, free_udp_port(), self.boot_addr, ttl=self.TTL)
        a.start()
        self.agents.append(a)
        return a


# ── 4. End-to-end bind over DHT ──────────────────────────────────────────────────

class TestEndToEndBind(DHTSwarmBase):

    def test_discover_by_name_and_bind_without_prior_address(self):
        device = self.make_device("edge-cam", ["compute", "sensing"])
        agent = self.make_agent("seeker")
        time.sleep(0.6)                                 # bootstrap + publish settle

        # Agent has NO prior knowledge of the device's TCP address.
        with agent.swarm._lock:
            self.assertNotIn(device.node_id, agent.swarm._peers,
                             "agent must not know the device address up front")

        found = agent.find_capability("sensing")
        names = {r["name"] for r in found}
        self.assertIn("sensing", names, "discovered capability by name over DHT")
        rec = next(r for r in found if r["name"] == "sensing")
        self.assertEqual(rec["node_id"], device.node_id)

        # Address was learned purely from the DHT record.
        with agent.swarm._lock:
            self.assertIn(device.node_id, agent.swarm._peers,
                          "device address resolved from DHT, not prior knowledge")

        result = agent.bind_remote("sensing", priority=5)
        self.assertTrue(result.get("verified"), f"bind not verified: {result}")
        self.assertEqual(result.get("provider_node_id"), device.node_id)

        # And a real data pull works over the bound channel.
        reading = agent.request_data(result, "sensing")
        self.assertEqual(reading.get("type"), "reading", f"bad reading: {reading}")

    def test_bind_remote_to_specific_provider(self):
        d1 = self.make_device("cam-1", ["compute", "sensing"])
        d2 = self.make_device("cam-2", ["compute", "sensing"])
        agent = self.make_agent("seeker2")
        time.sleep(0.7)

        agent.find_capability("sensing")                # populate agent records/peers
        r2 = agent.bind_remote_to(d2.node_id, "sensing", priority=5)
        self.assertTrue(r2.get("verified"))
        self.assertEqual(r2.get("provider_node_id"), d2.node_id,
                         "bound the specifically targeted provider")


# ── 5. Node departure ────────────────────────────────────────────────────────────

class TestNodeDeparture(DHTSwarmBase):
    TTL = 2

    def test_records_expire_after_device_stops(self):
        device = self.make_device("temp-sensor", ["compute", "sensing"])
        agent = self.make_agent("watcher")
        time.sleep(0.6)

        self.assertTrue(agent.find_capability("sensing"), "present while device is up")

        device.stop_swarm()
        self.devices.remove(device)
        time.sleep(self.TTL + 1.2)                       # exceed TTL with no re-store

        remaining = [r for r in agent.find_capability("sensing")
                     if r["node_id"] == device.node_id]
        self.assertEqual(remaining, [],
                         "departed device's record expired from discovery")


if __name__ == "__main__":
    unittest.main(verbosity=2)
