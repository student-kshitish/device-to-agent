"""
d2a/swarm_dht.py — DHTSwarm: Kademlia UDP discovery + reused LANSwarm TCP core.

DHTSwarm is a SwarmTransport that swaps LANSwarm's UDP-broadcast discovery for a
real Kademlia DHT (d2a/kademlia.py) while REUSING LANSwarm's TCP messaging core
verbatim — the TCP server, send()/send_and_recv(), the (node_id,name)->record
`records` cache, the `_peers` address table, `_lock`, add_known_peer(), and
probe_peer(). Because it subclasses LANSwarm, RemoteAgent.bind_remote() /
bind_remote_to() — which reach directly into `self.swarm._lock` and
`self.swarm.records.values()` — work UNCHANGED over the DHT.

Discovery over the DHT:
  publish(record)      -> DHT STORE under "cap:<name>" and "node:<node_id>",
                          plus the same local records/_peers caching LANSwarm does.
  discover(cap_name)   -> DHT FIND_VALUE "cap:<name>", merge results into
                          records/_peers, return the live filtered list.
  send(node_id, ...)   -> inherited LANSwarm TCP; if the address isn't cached,
                          resolve it first via DHT "node:<node_id>".

Node identity: the D2A 64-bit node_id is hashed (SHA-1) into the 160-bit DHT
keyspace for routing only; messaging still uses the original node_id + address
carried inside each record. identity.py is untouched.

Ports are fully parameterizable: `dht_port` (UDP) and `tcp_port` (TCP, 0 = OS
picks) — so N nodes run on one machine.

TECH DEBT (intentional, see report): to keep bind_remote() unchanged we
replicate LANSwarm's `.records`/`_lock` here rather than adding a first-class
`get_provider_record()` to the SwarmTransport ABC. The future fix is that ABC
method so consumers stop reaching into transport internals.
"""

import time

from d2a.swarm import LANSwarm
from d2a.kademlia import KademliaNode
from d2a.protocol import stamp


class DHTSwarm(LANSwarm):
    """
    SwarmTransport backed by a Kademlia DHT for discovery and the LANSwarm TCP
    core for messaging. Drop-in for LANSwarm; agents/runtimes don't know which
    transport is underneath.

    Args:
        node_id:   D2A node_id (hashed into the DHT keyspace for routing).
        dht_port:  UDP port for the Kademlia node (distinct per node on one host).
        bootstrap: (ip, port) or "ip:port" of a known DHT node to join, or None
                   for the first/bootstrap node.
        host:      bind address for both UDP and TCP (default 0.0.0.0).
        tcp_port:  TCP messaging port (0 = OS-assigned free port).
        ttl:       record TTL in seconds (multi-value prune horizon).
        verbose:   trace DHT messages.
    """

    def __init__(
        self,
        node_id: str,
        dht_port: int,
        bootstrap: tuple[str, int] | str | None = None,
        host: str = "0.0.0.0",
        tcp_port: int = 0,
        ttl: int = 30,
        verbose: bool = False,
    ):
        # Reuse LANSwarm's TCP setup (binds the TCP server on tcp_port). We do
        # NOT use its UDP broadcast discovery — discovery_port is irrelevant here
        # and its _udp_loop is never started because we override start().
        super().__init__(node_id=node_id, host=host, port=tcp_port)

        self.dht_port = dht_port
        self.ttl = ttl
        self._bootstrap = self._parse_bootstrap(bootstrap)
        self._dht = KademliaNode(
            node_id=node_id, udp_port=dht_port, host=host, ttl=ttl, verbose=verbose,
        )

    @staticmethod
    def _parse_bootstrap(bootstrap) -> tuple[str, int] | None:
        if bootstrap is None:
            return None
        if isinstance(bootstrap, str):
            ip, port = bootstrap.rsplit(":", 1)
            return (ip, int(port))
        return (bootstrap[0], int(bootstrap[1]))

    # ── lifecycle: TCP core from LANSwarm, discovery from Kademlia ────────────────

    def start(self) -> None:
        # Start ONLY the TCP message server (reused from LANSwarm) — not the UDP
        # broadcast listener. Then bring up the Kademlia DHT node.
        self._running = True
        import threading
        threading.Thread(target=self._tcp_loop, daemon=True).start()
        self._dht.start(bootstrap=self._bootstrap)

    def stop(self) -> None:
        self._dht.stop()
        super().stop()                                     # closes TCP server

    # ── discovery via DHT ─────────────────────────────────────────────────────────

    def publish(self, record: dict) -> None:
        rec = dict(record)
        rec.setdefault("ts", time.time())
        stamp(rec)                                     # record carries its author's version
        nid = rec.get("node_id", "")
        name = rec.get("name", "")

        # Same local caching LANSwarm does — keeps bind_remote()'s local fast path
        # and _peers table populated for our own records.
        with self._lock:
            self.records[(nid, name)] = rec
            if nid and rec.get("address"):
                self._peers[nid] = tuple(rec["address"])

        # Publish into the DHT: by capability name and by node id (address resolution).
        if name:
            self._dht.store(f"cap:{name}", rec)
        if nid:
            self._dht.store(f"node:{nid}", rec)

    def discover(self, capability_name: str = None) -> list[dict]:
        if capability_name is not None:
            found = self._dht.find_value(f"cap:{capability_name}")
            self._absorb(found)
        # No wildcard enumeration in a DHT — for the all-capabilities case we
        # return whatever we've already cached (see known gaps in the report).
        # Filter by THIS transport's ttl (not LANSwarm's global TTL) so departed
        # providers age out of discovery on the configured horizon.
        now = time.time()
        with self._lock:
            return [
                dict(r) for r in self.records.values()
                if now - r.get("ts", 0) <= self.ttl
                and (capability_name is None or r.get("name") == capability_name)
            ]

    def unpublish(self, record: dict) -> None:
        """
        Graceful departure: drop this record from the local cache and tombstone it
        in the DHT (by capability key and by node key) so discover() stops returning
        it immediately — no waiting a full TTL for it to age out. See
        KademliaNode.remove for the tombstone mechanism.
        """
        nid = record.get("node_id", "")
        name = record.get("name", "")
        with self._lock:
            self.records.pop((nid, name), None)
        # Carry the capability name inside the tombstone so a receiving node can
        # locate the record in its (node_id, name)-keyed cache and drop it.
        if name:
            self._dht.remove(f"cap:{name}", nid, {"name": name})
        if nid:
            self._dht.remove(f"node:{nid}", nid, {"name": name})

    def _absorb(self, records: list[dict]) -> None:
        """Merge DHT-discovered records into the local records + peer tables. A
        tombstone (graceful-departure marker) instead REMOVES the provider's cached
        record, so a discover() that observes the tombstone drops the provider now."""
        with self._lock:
            for r in records:
                nid = r.get("node_id", "")
                name = r.get("name", "")
                if not nid:
                    continue
                if r.get("tombstone"):
                    self.records.pop((nid, name), None)
                    continue
                self.records[(nid, name)] = dict(r)
                if r.get("address"):
                    self._peers[nid] = tuple(r["address"])

    # ── messaging: reuse LANSwarm TCP, add DHT address resolution ─────────────────

    def _resolve_peer(self, target_node_id: str) -> None:
        """If we don't know the target's address, resolve it via the DHT."""
        with self._lock:
            known = target_node_id in self._peers
        if known:
            return
        found = self._dht.find_value(f"node:{target_node_id}")
        self._absorb(found)

    def send(self, target_node_id: str, message: dict) -> bool:
        self._resolve_peer(target_node_id)
        return super().send(target_node_id, message)

    def send_and_recv(self, target_node_id: str, message: dict, timeout: float = 5.0):
        self._resolve_peer(target_node_id)
        return super().send_and_recv(target_node_id, message, timeout=timeout)

    # ── introspection (handy for demos/tests) ─────────────────────────────────────

    @property
    def dht_address(self) -> tuple[str, int]:
        return (self._dht._own_ip(), self.dht_port)

    def routing_size(self) -> int:
        return self._dht.routing_table.size()
