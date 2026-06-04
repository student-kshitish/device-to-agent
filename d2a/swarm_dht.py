"""
d2a/swarm_dht.py — EdgeMind DHT adapter stub.

Thin adapter that maps the frozen SwarmTransport interface onto EdgeMind's
existing Kademlia DHT + NAT traversal. Zero DHT logic lives here — this file
is only an interface bridge and TODO map.

Usage (once anp-edge-swarm is installed):
    from d2a.swarm_dht import DHTSwarm
    transport = DHTSwarm(node_id=runtime.node_id, bootstrap_peers=["ip:port"])
    runtime = DeviceRuntime(name="mynode", transport=transport)

Same interface as LANSwarm — agents and runtimes don't know or care which
transport is underneath.
"""

from d2a.swarm import SwarmTransport


class DHTSwarm(SwarmTransport):
    """
    SwarmTransport adapter for the EdgeMind Kademlia DHT.

    Requires: pip install anp-edge-swarm
    Repo:     github.com/student-kshitish/anp-edge-swarm

    If the package is absent, raises ImportError with a clear friendly message.
    No DHT logic is reimplemented here.
    """

    def __init__(self, node_id: str, bootstrap_peers: list[str] | None = None, **kwargs):
        self.node_id = node_id
        self.bootstrap_peers = bootstrap_peers or []
        try:
            from anp_edge_swarm import dht  # type: ignore
            self._dht = dht
        except ImportError:
            raise ImportError(
                "EdgeMind swarm not installed.\n"
                "  • For LAN / single-machine use: LANSwarm (built-in, no install needed)\n"
                "  • For cross-network DHT + NAT traversal:\n"
                "      pip install anp-edge-swarm\n"
                "    then use DHTSwarm(node_id=..., bootstrap_peers=[...])"
            )
        # TODO: self._node = self._dht.KademliaNode(node_id=node_id,
        #           bootstrap=bootstrap_peers)

    def start(self) -> None:
        # TODO: self._node.start()
        raise NotImplementedError("DHTSwarm.start — wire to self._dht.KademliaNode.start()")

    def stop(self) -> None:
        # TODO: self._node.stop()
        raise NotImplementedError("DHTSwarm.stop — wire to self._dht.KademliaNode.stop()")

    def publish(self, record: dict) -> None:
        # TODO: self._dht.store(
        #     key=f"cap:{record['name']}",
        #     value=record,
        #     node=self._node,
        # )
        raise NotImplementedError("DHTSwarm.publish — wire to self._dht.store()")

    def discover(self, capability_name: str = None) -> list[dict]:
        # TODO: key = f"cap:{capability_name}" if capability_name else "cap:*"
        #       return self._dht.find_value(key=key, node=self._node)
        raise NotImplementedError("DHTSwarm.discover — wire to self._dht.find_value()")

    def send(self, target_node_id: str, message: dict) -> bool:
        # TODO: return self._dht.send_to_peer(
        #     target_node_id=target_node_id,
        #     message=message,
        #     node=self._node,
        #     hole_punch=True,   # EdgeMind NAT traversal
        # )
        raise NotImplementedError("DHTSwarm.send — wire to self._dht.send_to_peer() with hole_punch=True")
