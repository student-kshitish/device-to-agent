"""
agents/simple_agent.py — friendly agent wrapper for the 5-line experience.

Usage:
    agent = Agent("my-agent", needs=["compute", "camera"])
    agent.start()
    agent.seed_provider(runtime)   # loopback/testing; on a real LAN, omit this

    with agent.use("compute") as r:
        frame = r.data()
        print(frame["frame"]["raw"])

    agent.stop()

ResourceHandle.data() calls request_data() (the on-demand default).
ResourceHandle auto-releases on __exit__ so the binding is freed.
"""

import time

from agents.remote_agent import RemoteAgent


class ResourceHandle:
    """
    Returned by Agent.use(). Wraps a live binding.

    .data()    — on-demand pull: fresh frame right now (the default path).
    .release() — return the resource to the provider; called automatically by the context manager.
    """

    def __init__(self, binding: dict, remote: RemoteAgent) -> None:
        self._binding = binding
        self._remote  = remote
        self._released = False

    def data(self, capability: str = None) -> dict:
        """Fetch one fresh data frame from the device. This is the primary method."""
        return self._remote.request_data(self._binding, capability)

    def release(self) -> None:
        """Release the binding back to the provider."""
        if not self._released:
            self._released = True
            try:
                self._remote.release_binding(self._binding)
            except Exception:
                pass

    def binding(self) -> dict:
        """Expose the raw binding dict for advanced use."""
        return self._binding

    # context-manager support: auto-release on exit
    def __enter__(self) -> "ResourceHandle":
        return self

    def __exit__(self, *_) -> None:
        self.release()


class Agent:
    """
    Thin, friendly wrapper around RemoteAgent.

    Designed for the common case: find a resource on the network, use it, release it.
    Handles discovery, binding, and teardown so the agent author writes ~5 lines.

    needs: list of resource names this agent wants, e.g. ["compute", "storage", "camera"]
    """

    def __init__(self, name: str, needs: list[str]) -> None:
        self.name  = name
        self.needs = needs
        self._remote = RemoteAgent(name=name)

    def start(self) -> None:
        """Start the swarm transport (required before find() or use())."""
        self._remote.needs = list(self.needs)
        self._remote.start()

    def stop(self) -> None:
        """Stop the swarm transport and free resources."""
        self._remote.stop()

    def seed_provider(self, runtime) -> None:
        """
        Inject a known provider's capabilities into local discovery cache.
        Use this for loopback/testing when UDP broadcast is not available.
        On a real LAN, call find() after start() and the swarm discovers naturally.
        """
        ip, port = runtime.swarm.address
        self._remote.swarm.add_known_peer(runtime.node_id, ip, port)
        now = time.time()
        for cap in runtime.advertise():
            self._remote.swarm.records[(runtime.node_id, cap.name)] = {
                "node_id":    runtime.node_id,
                "name":       cap.name,
                "tags":       list(cap.tags),
                "live_state": dict(cap.live_state),
                "public_key": runtime.public_key,
                "address":    [ip, port],
                "ts":         now,
            }

    def find(self) -> list[dict]:
        """
        Discover providers offering any of this agent's needs.
        Returns list of dicts with "provider_node_id", "resource_name", "record".
        """
        results: list = []
        now = time.time()
        for need in self.needs:
            with self._remote.swarm._lock:
                cached = [
                    {
                        "provider_node_id": r["node_id"],
                        "resource_name":    r["name"],
                        "record":           dict(r),
                    }
                    for r in self._remote.swarm.records.values()
                    if r.get("name") == need
                    and r.get("node_id") != self._remote.agent_id
                    and now - r.get("ts", 0) <= 30
                ]
            results.extend(cached)
        return results

    def use(self, resource_name: str, priority: int = 5) -> ResourceHandle:
        """
        Bind to a provider offering `resource_name` and return a ResourceHandle.

        If the bind is denied by device policy the handle is still returned but
        .data() will fail — check handle.binding()["status"] == "denied" before use.
        For typical agent code, just call .data() and handle the error dict.

        The recommended pattern is the context manager so release is automatic:
            with agent.use("compute") as r:
                frame = r.data()
        """
        binding = self._remote.bind_remote(resource_name, priority)
        return ResourceHandle(binding, self._remote)
