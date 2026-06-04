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
from typing import Any

from agents.remote_agent import RemoteAgent
from d2a.composer import Composer, Composition


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
        # In-process seeded runtimes (populated via seed_provider)
        self._seeded_runtimes: dict[str, Any] = {}   # node_id → runtime
        self._cap_contracts: dict[tuple, Any] = {}   # (node_id, cap_name) → CapabilityContract

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
        Also registers the runtime for in-process binding (no TCP needed).
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
        # Register for in-process binding + contracts
        self._seeded_runtimes[runtime.node_id] = runtime
        if hasattr(runtime, "capability_contracts"):
            for cap_name, cc in runtime.capability_contracts.items():
                self._cap_contracts[(runtime.node_id, cap_name)] = cc

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

    def achieve(self, goal: str, priority: int = 5):
        """
        High-level goal API.  Returns a Composition (or (False, reason) on failure).

        In-process mode (when runtimes were seeded via seed_provider):
            Uses direct broker calls — no TCP/swarm needed.
        Network mode (no seeded runtimes):
            Uses bind_remote_to / release_binding via swarm.

        Pattern:
            with agent.achieve("vision") as comp:
                result = comp.run()
        """
        if self._seeded_runtimes:
            return self._achieve_inprocess(goal, priority)
        return self._achieve_remote(goal, priority)

    def _achieve_inprocess(self, goal: str, priority: int):
        """Bind via direct broker calls using seeded runtime references."""
        pool = self._build_pool_from_seeded()
        if not pool:
            return False, "no seeded providers available for composition"

        agent_id = self._remote.agent_id
        runtimes = self._seeded_runtimes

        def bind_fn(node_id, cap_name, p):
            rt = runtimes.get(node_id)
            if rt is None:
                return {"status": "error", "message": f"no runtime for {node_id}"}
            result = rt.broker_request(agent_id, cap_name, [], p)
            if result.get("status") == "queued":
                # Cancel immediately: AtomicBinder uses fail-fast semantics.
                rt.broker.cancel_queue(agent_id, cap_name)
                result["status"] = "busy"
            result["provider_node_id"] = node_id
            result["capability_name"]  = cap_name
            return result

        def release_fn(binding):
            node_id  = binding.get("provider_node_id", "")
            cap_name = binding.get("capability_name", "")
            rt = runtimes.get(node_id)
            if rt:
                rt.broker_release(agent_id, cap_name)

        def health_fn(binding):
            node_id    = binding.get("provider_node_id", "")
            rt         = runtimes.get(node_id)
            binding_id = binding.get("binding_id", "")
            if rt is None:
                return {"verdict": "error", "healthy": False}
            b = rt.broker.get_binding(binding_id)
            if b is None or b.status != "active":
                return {"verdict": "expired", "healthy": False}
            return {"verdict": "comfort", "healthy": True}

        def data_fn(binding):
            node_id  = binding.get("provider_node_id", "")
            cap_name = binding.get("capability_name", "")
            rt = runtimes.get(node_id)
            if rt:
                return rt.data.get_reading(cap_name)
            return {}

        composer = Composer(
            capability_pool_provider=lambda: pool,
            bind_fn=bind_fn,
            release_fn=release_fn,
            health_fn=health_fn,
            data_fn=data_fn,
        )
        plan = composer.plan(goal)
        if not plan.ok:
            return False, f"PLAN FAILED: {plan.reason}"

        result = composer.bind(plan, priority=priority)
        if isinstance(result, tuple):
            return result  # (False, reason)

        # Attach run/release to the composition via the composer
        comp = result
        comp._composer = composer
        return comp

    def _achieve_remote(self, goal: str, priority: int):
        """Bind via network (swarm TCP). Requires swarm to be started."""
        pool = self._build_pool_from_swarm()
        if not pool:
            return False, "no network providers discovered"

        remote = self._remote

        def bind_fn(node_id, cap_name, p):
            result = remote.bind_remote_to(node_id, cap_name, p)
            result["provider_node_id"] = node_id
            result["capability_name"]  = cap_name
            return result

        def release_fn(binding):
            remote.release_binding(binding)

        def health_fn(binding):
            # Light: check binding still holds (no expired_at breach)
            import time
            expires = binding.get("expires_at", 0)
            if expires and time.time() > expires:
                return {"verdict": "expired", "healthy": False}
            return {"verdict": "comfort", "healthy": True}

        def data_fn(binding):
            cap = binding.get("capability_name", "")
            return remote.request_data(binding, cap)

        composer = Composer(
            capability_pool_provider=lambda: pool,
            bind_fn=bind_fn,
            release_fn=release_fn,
            health_fn=health_fn,
            data_fn=data_fn,
        )
        plan = composer.plan(goal)
        if not plan.ok:
            return False, f"PLAN FAILED: {plan.reason}"

        result = composer.bind(plan, priority=priority)
        if isinstance(result, tuple):
            return result
        comp = result
        comp._composer = composer
        return comp

    def _build_pool_from_seeded(self) -> list:
        """Build a capability pool from seeded runtimes + their contracts."""
        pool = []
        now = time.time()
        for (node_id, cap_name), cc in self._cap_contracts.items():
            rec = self._remote.swarm.records.get((node_id, cap_name))
            if rec is None or now - rec.get("ts", 0) > 60:
                continue
            io_contract = cc.produces if cc.role == "producer" else cc.accepts
            if io_contract is None:
                continue
            pool.append({
                "node_id":      node_id,
                "capability":   cap_name,
                "role":         cc.role,
                "contract":     io_contract,
                "device_class": rec.get("device_class", "unknown"),
                "live_state":   rec.get("live_state", {}),
            })
        return pool

    def _build_pool_from_swarm(self) -> list:
        """Build pool from swarm-discovered records. Contracts inferred from names."""
        from d2a.contracts import IOContract
        _DEFAULT_CONTRACTS = {
            "camera":     ("producer", IOContract(media="image",  format="unknown")),
            "microphone": ("producer", IOContract(media="audio",  format="pcm16")),
            "gpu":        ("consumer", IOContract(media="tensor", format="float32",
                                                  shape=(640, 480, 3))),
            "compute":    ("consumer", IOContract(media="tensor", format="float32",
                                                  shape=(640, 480, 3))),
        }
        pool = []
        now = time.time()
        with self._remote.swarm._lock:
            records = list(self._remote.swarm.records.values())
        for rec in records:
            if rec.get("node_id") == self._remote.agent_id:
                continue
            if now - rec.get("ts", 0) > 30:
                continue
            cap_name = rec.get("name", "")
            if cap_name not in _DEFAULT_CONTRACTS:
                continue
            role, io_contract = _DEFAULT_CONTRACTS[cap_name]
            pool.append({
                "node_id":      rec["node_id"],
                "capability":   cap_name,
                "role":         role,
                "contract":     io_contract,
                "device_class": rec.get("device_class", "unknown"),
                "live_state":   rec.get("live_state", {}),
            })
        return pool

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
