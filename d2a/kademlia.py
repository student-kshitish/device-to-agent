"""
d2a/kademlia.py — pure-stdlib Kademlia DHT node for D2A discovery.

This is the UDP *discovery* layer only. Actual capability messaging (bind,
get_reading, streams) rides the reused LANSwarm TCP core — see swarm_dht.py.

Relationship to the anp-edge-swarm reference (github.com/student-kshitish/
anp-edge-swarm, swarm/kademlia_node.py + swarm/kbucket.py):
  We reuse the *shape* — the K-bucket XOR routing table and the JSON-over-UDP
  message vocabulary (PING/PONG, FIND_NODE/FOUND_NODES, STORE, FIND_VALUE/VALUE).
  We deliberately DIVERGE on four points the reference gets wrong for our needs:
    1. Port is a constructor arg, not a module constant + singleton
       (reference binds a hardcoded 6881 via one process-global node).
    2. Storage is MULTI-VALUE with TTL: key -> {provider_id: record}, pruned
       on age (reference overwrites a single value per key, never expires).
    3. find_value is EVENT-DRIVEN with early-exit (reference blind-sleeps 2 s).
    4. All shared state is guarded by a lock (reference has no synchronization).

Keys are arbitrary strings ("cap:compute", "node:<id>"); both the local D2A
node_id and every key string are hashed with SHA-1 into the 160-bit id space
for XOR distance. Records carry their own "ts" (the provider's publish time),
which is the single source of truth for expiry — so a departed provider's
records age out everywhere TTL seconds after its last re-store, regardless of
how many hops they propagated.
"""

import hashlib
import json
import socket
import threading
import time

from d2a.protocol import (
    PROTOCOL_VERSION, VERSION_FIELD, stamp, classify, versions_compatible,
    warn_legacy_once, logger as _plog,
)

K = 20        # max nodes per bucket / replication fan-out
ALPHA = 3     # lookup parallelism (advisory; we fan out to K on tiny nets)
ID_BITS = 160
MAX_PACKET = 65535


def hash_id(s: str) -> str:
    """Map any string (node_id or key) into the 160-bit hex id space."""
    return hashlib.sha1(s.encode()).hexdigest()


def xor_distance(id1: str, id2: str) -> int:
    """XOR distance between two 40-char hex ids."""
    return int(id1, 16) ^ int(id2, 16)


# ── routing table ───────────────────────────────────────────────────────────────

class KBucket:
    """
    One k-bucket. nodes: list of (routing_id, ip, port, last_seen), oldest at
    head (LRU). Full bucket evicts the oldest — the reference's behavior; we do
    not ping-before-evict (acceptable for a discovery overlay, not a store).
    """

    def __init__(self):
        self.nodes: list = []

    def add(self, rid: str, ip: str, port: int) -> None:
        for i, (existing, _, _, _) in enumerate(self.nodes):
            if existing == rid:
                self.nodes.pop(i)
                self.nodes.append((rid, ip, port, time.time()))
                return
        if len(self.nodes) < K:
            self.nodes.append((rid, ip, port, time.time()))
        else:
            self.nodes.pop(0)                       # evict oldest
            self.nodes.append((rid, ip, port, time.time()))

    def evict_stale(self, max_age: float) -> None:
        now = time.time()
        self.nodes = [n for n in self.nodes if now - n[3] <= max_age]


class RoutingTable:
    """
    160-bucket Kademlia routing table keyed on XOR distance from own routing id.
    Stores (routing_id, ip, port) — the routing_id is hash_id(d2a_node_id), but
    the ip/port point at the peer's UDP DHT socket.
    """

    def __init__(self, own_routing_id: str):
        self.own_id = own_routing_id
        self.buckets = [KBucket() for _ in range(ID_BITS)]
        self._lock = threading.Lock()

    def _bucket_index(self, rid: str) -> int:
        dist = xor_distance(self.own_id, rid)
        if dist == 0:
            return 0
        return dist.bit_length() - 1

    def add_node(self, rid: str, ip: str, port: int) -> None:
        if rid == self.own_id:
            return
        with self._lock:
            self.buckets[self._bucket_index(rid)].add(rid, ip, port)

    def find_closest(self, target_id: str, count: int = K) -> list:
        with self._lock:
            all_nodes = [n for b in self.buckets for n in b.nodes]
        return sorted(all_nodes, key=lambda n: xor_distance(n[0], target_id))[:count]

    def all_nodes(self) -> list:
        with self._lock:
            return [n for b in self.buckets for n in b.nodes]

    def evict_stale(self, max_age: float) -> None:
        with self._lock:
            for b in self.buckets:
                b.evict_stale(max_age)

    def size(self) -> int:
        with self._lock:
            return sum(len(b.nodes) for b in self.buckets)


# ── DHT node ────────────────────────────────────────────────────────────────────

class KademliaNode:
    """
    A single Kademlia DHT node over one UDP socket.

    Parameters
    ----------
    node_id     : the D2A 64-bit node_id (hashed to 160-bit for routing).
    udp_port    : UDP port to bind for DHT traffic (parameterizable — N nodes
                  on one machine use distinct ports).
    host        : bind address (default 0.0.0.0).
    ttl         : seconds a stored record stays live without a re-store.
    refresh_interval : how often to re-store own records / evict stale / re-ping.
                       Defaults to ttl/2 so live records never lapse mid-run.
    verbose     : print per-message trace (off by default; the reference spams).

    Storage layout (multi-value):
        storage[key_str] = { provider_id: record_dict }
    record_dict carries "ts" = provider publish time; expiry is driven by it.
    """

    def __init__(
        self,
        node_id: str,
        udp_port: int,
        host: str = "0.0.0.0",
        ttl: int = 30,
        refresh_interval: float | None = None,
        verbose: bool = False,
    ):
        self.node_id = node_id
        self.routing_id = hash_id(node_id)
        self.udp_port = udp_port
        self.host = host
        self.ttl = ttl
        self.refresh_interval = refresh_interval if refresh_interval is not None else max(1.0, ttl / 2)
        self.verbose = verbose

        self.routing_table = RoutingTable(self.routing_id)
        self._lock = threading.Lock()
        self.storage: dict[str, dict[str, dict]] = {}      # key -> {provider_id: record}
        self._own_records: dict[str, dict] = {}            # key -> own record (for re-store)
        self._value_events: dict[str, threading.Event] = {}  # key -> waiter

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────────────────

    def start(self, bootstrap: tuple[str, int] | None = None) -> None:
        self._sock.bind((self.host, self.udp_port))
        self._sock.settimeout(1.0)
        self._running = True
        threading.Thread(target=self._listen_loop, daemon=True,
                         name=f"kad-listen-{self.udp_port}").start()
        threading.Thread(target=self._refresh_loop, daemon=True,
                         name=f"kad-refresh-{self.udp_port}").start()
        if bootstrap:
            self._bootstrap(bootstrap)

    def stop(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass

    def _bootstrap(self, bootstrap: tuple[str, int]) -> None:
        """Send FIND_NODE for our own id to the bootstrap peer to join the mesh."""
        bip, bport = bootstrap
        self._send(bip, bport, {
            "type": "FIND_NODE",
            "sender_id": self.node_id,
            "sender_ip": self._own_ip(),
            "sender_port": self.udp_port,
            "target": self.routing_id,
        })

    # ── network I/O ──────────────────────────────────────────────────────────────

    def _own_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _send(self, ip: str, port: int, msg: dict) -> None:
        try:
            self._sock.sendto(json.dumps(stamp(msg), default=str).encode(), (ip, port))
            if self.verbose:
                print(f"[kad:{self.udp_port}] -> {msg.get('type')} {ip}:{port}", flush=True)
        except Exception as e:
            if self.verbose:
                print(f"[kad:{self.udp_port}] send err {ip}:{port}: {e}", flush=True)

    def _listen_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._sock.recvfrom(MAX_PACKET)
            except socket.timeout:
                continue
            except OSError:
                break                                       # socket closed on stop()
            try:
                text = data.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if not text.startswith("{"):
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if "type" in msg:
                try:
                    self._handle(msg, addr)
                except Exception as e:
                    if self.verbose:
                        print(f"[kad:{self.udp_port}] handle err: {e}", flush=True)

    def _refresh_loop(self) -> None:
        while self._running:
            time.sleep(self.refresh_interval)
            if not self._running:
                break
            # 1. re-store our own records with a fresh ts so they never lapse
            with self._lock:
                own = list(self._own_records.items())
            for key, record in own:
                record = dict(record)
                record["ts"] = time.time()
                with self._lock:
                    self._own_records[key] = record
                self._store(key, record)
            # 2. prune expired values everywhere
            self._prune()
            # 3. evict stale routing entries
            self.routing_table.evict_stale(max(self.ttl, self.refresh_interval * 3))

    # ── storage helpers ──────────────────────────────────────────────────────────

    def _merge_record(self, key: str, record: dict) -> None:
        """Merge one record into multi-value storage, keeping the freshest ts."""
        pid = record.get("node_id", "")
        if not pid:
            return
        rec_v = record.get(VERSION_FIELD)
        if rec_v is not None and not versions_compatible(rec_v, PROTOCOL_VERSION):
            # A same-major peer can hand us a record authored by a different-major
            # node (relay). We ingest it — record-level v is the eventual gate.
            _plog.debug("DHT: ingesting foreign-major record v=%s for key=%s", rec_v, key)
        with self._lock:
            bucket = self.storage.setdefault(key, {})
            existing = bucket.get(pid)
            if existing is None or record.get("ts", 0) >= existing.get("ts", 0):
                bucket[pid] = dict(record)

    def _live_records(self, key: str) -> list[dict]:
        now = time.time()
        with self._lock:
            bucket = self.storage.get(key, {})
            return [dict(r) for r in bucket.values() if now - r.get("ts", 0) <= self.ttl]

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            for key in list(self.storage.keys()):
                bucket = self.storage[key]
                for pid in list(bucket.keys()):
                    if now - bucket[pid].get("ts", 0) > self.ttl:
                        del bucket[pid]
                if not bucket:
                    del self.storage[key]

    # ── public API ───────────────────────────────────────────────────────────────

    def store(self, key: str, record: dict) -> None:
        """
        Publish `record` under `key`. Kept locally, replicated to the K closest
        nodes, and remembered so the refresh loop re-stores it (keeps it live).
        """
        record = dict(record)
        record.setdefault("ts", time.time())
        with self._lock:
            self._own_records[key] = record
        self._store(key, record)

    def remove(self, key: str, provider_id: str, extra: dict | None = None) -> None:
        """
        Graceful unpublish of `provider_id`'s value under `key`. Kademlia has no
        native DELETE, so we publish a short-lived TOMBSTONE: a record with a fresh
        `ts` (so it SUPERSEDES the live record in every merge — merge keeps the
        freshest ts) carrying a `tombstone` flag, and we stop refreshing the
        original (pop from `_own_records`). It replicates to the K closest exactly
        like a normal store. Consumers drop the provider the moment they see the
        tombstone; the tombstone itself is TTL-pruned like any other record, so
        storage does not grow. This is the honest removal primitive for a
        store-and-forward DHT — a departed provider disappears from find_value
        immediately instead of aging out over a full TTL.
        """
        with self._lock:
            self._own_records.pop(key, None)
        tomb = {"node_id": provider_id, "key": key, "tombstone": True,
                "ts": time.time(), VERSION_FIELD: PROTOCOL_VERSION}
        if extra:
            tomb.update(extra)   # e.g. the capability name, so consumers can locate the cached record
        self._store(key, tomb)

    def _store(self, key: str, record: dict) -> None:
        self._merge_record(key, record)
        target = hash_id(key)
        for rid, nip, nport, _ in self.routing_table.find_closest(target, K):
            self._send(nip, nport, {
                "type": "STORE",
                "sender_id": self.node_id,
                "key": key,
                "records": [record],
            })

    def find_value(self, key: str, timeout: float = 2.0, settle: float = 0.15) -> list[dict]:
        """
        Look up all live records under `key`. Event-driven with early-exit:
        fans FIND_VALUE out to the K closest nodes, then returns as soon as
        results stop arriving (quiet for `settle` s) — or at `timeout`.
        Always merges whatever we already hold locally.
        """
        target = hash_id(key)
        ev = threading.Event()
        with self._lock:
            self._value_events[key] = ev

        queried: set = set()
        for rid, nip, nport, _ in self.routing_table.find_closest(target, K):
            self._send(nip, nport, {
                "type": "FIND_VALUE",
                "sender_id": self.node_id,
                "sender_ip": self._own_ip(),
                "sender_port": self.udp_port,
                "key": key,
            })
            queried.add(rid)

        deadline = time.time() + timeout
        last_count = -1
        while time.time() < deadline:
            got = ev.wait(settle)
            ev.clear()
            cur = len(self._live_records(key))
            if not got and cur == last_count:
                break                                       # no new arrivals, settled
            last_count = cur

        with self._lock:
            self._value_events.pop(key, None)
        return self._live_records(key)

    def find_node(self, target_routing_id: str, timeout: float = 2.0) -> list:
        """Iterative-ish node lookup — fan FIND_NODE out and let handlers walk."""
        for rid, nip, nport, _ in self.routing_table.find_closest(target_routing_id, K):
            self._send(nip, nport, {
                "type": "FIND_NODE",
                "sender_id": self.node_id,
                "sender_ip": self._own_ip(),
                "sender_port": self.udp_port,
                "target": target_routing_id,
            })
        time.sleep(min(timeout, 0.5))
        return self.routing_table.find_closest(target_routing_id, K)

    # ── message handler ──────────────────────────────────────────────────────────

    def _handle(self, msg: dict, addr) -> None:
        # ── protocol version gate (single inbound UDP chokepoint) ──
        kind = classify(msg.get(VERSION_FIELD))
        if kind == "incompatible":
            if self.verbose:
                print(f"[kad:{self.udp_port}] drop foreign-major {msg.get('type')} from {addr[0]}", flush=True)
            _plog.debug("DHT: dropping foreign-major %s from %s", msg.get("type"), addr[0])
            return                                     # drop, no reply (no error loops)
        if kind == "legacy":
            warn_legacy_once(f"dht:{addr[0]}")

        sender_id = msg.get("sender_id")
        sender_ip = msg.get("sender_ip", addr[0])
        sender_port = msg.get("sender_port", addr[1])
        if sender_id:
            self.routing_table.add_node(hash_id(sender_id), sender_ip, int(sender_port))

        mtype = msg.get("type")

        if mtype == "PING":
            self._send(sender_ip, sender_port, {
                "type": "PONG", "sender_id": self.node_id,
                "sender_ip": self._own_ip(), "sender_port": self.udp_port,
            })

        elif mtype == "PONG":
            pass                                            # add_node above already refreshed

        elif mtype in ("FIND_NODE", "_walk"):
            target = msg.get("target", self.routing_id)
            closest = self.routing_table.find_closest(target, K)
            self._send(sender_ip, sender_port, {
                "type": "FOUND_NODES", "sender_id": self.node_id,
                "sender_ip": self._own_ip(), "sender_port": self.udp_port,
                "target": target,
                "nodes": [[rid, nip, nport] for rid, nip, nport, _ in closest],
            })

        elif mtype == "FOUND_NODES":
            target = msg.get("target", self.routing_id)
            for entry in msg.get("nodes", []):
                if len(entry) < 3:
                    continue
                rid, nip, nport = entry[0], entry[1], int(entry[2])
                if rid == self.routing_id:
                    continue
                known_before = any(rid == n[0] for n in self.routing_table.all_nodes())
                self.routing_table.add_node(rid, nip, nport)
                if not known_before:
                    # walk one hop toward target to warm the table
                    self._send(nip, nport, {
                        "type": "FIND_NODE", "sender_id": self.node_id,
                        "sender_ip": self._own_ip(), "sender_port": self.udp_port,
                        "target": target,
                    })

        elif mtype == "STORE":
            key = msg.get("key")
            if key is not None:
                for record in msg.get("records", []):
                    self._merge_record(key, record)

        elif mtype == "FIND_VALUE":
            key = msg.get("key")
            live = self._live_records(key) if key is not None else []
            if live:
                self._send(sender_ip, sender_port, {
                    "type": "VALUE", "sender_id": self.node_id,
                    "key": key, "records": live,
                })
            else:
                target = hash_id(key) if key else self.routing_id
                closest = self.routing_table.find_closest(target, K)
                self._send(sender_ip, sender_port, {
                    "type": "FOUND_NODES", "sender_id": self.node_id,
                    "sender_ip": self._own_ip(), "sender_port": self.udp_port,
                    "target": target,
                    "nodes": [[rid, nip, nport] for rid, nip, nport, _ in closest],
                })

        elif mtype == "VALUE":
            key = msg.get("key")
            if key is not None:
                for record in msg.get("records", []):
                    self._merge_record(key, record)
                with self._lock:
                    ev = self._value_events.get(key)
                if ev:
                    ev.set()
