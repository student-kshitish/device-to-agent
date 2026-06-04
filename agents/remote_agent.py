import time

from d2a import generate_node_id, LANSwarm, SwarmTransport

TTL = 30


class RemoteAgent:
    """
    Network-capable agent. Discovers providers via swarm transport,
    sends bind_request over TCP, verifies the response.

    Data delivery:
      request_data()  — DEFAULT. On-demand pull: one fresh frame per call.
      start_stream()  — OPT-IN.  Push loop at hz; frames arrive via on_frame callback.
      stop_stream()   — stops the push loop, back to zero background work.

    Works with any SwarmTransport — LANSwarm by default, DHTSwarm when installed.
    """

    def __init__(self, name: str = "agent", transport: SwarmTransport = None):
        self.name = name
        self.agent_id = generate_node_id()
        self.needs: list[str] = []
        self.swarm: SwarmTransport = transport if transport is not None else LANSwarm(node_id=self.agent_id)

        # streaming state: binding_id -> callback / device sub_id
        self._stream_handlers: dict[str, object]  = {}   # binding_id -> on_frame callable
        self._stream_sub_ids:  dict[str, str]      = {}   # binding_id -> device-side sub_id

    def start(self) -> None:
        self.swarm.start()
        # route incoming stream_frame messages to registered handlers
        self.swarm.message_handler = self._on_message

    def stop(self) -> None:
        self.swarm.stop()

    # ── incoming message router ────────────────────────────────────────────────

    def _on_message(self, message: dict) -> dict | None:
        """Handle messages pushed TO this agent (e.g. stream_frame from device)."""
        if message.get("type") == "stream_frame":
            bid     = message.get("binding_id", "")
            frame   = message.get("frame", {})
            handler = self._stream_handlers.get(bid)
            if handler:
                try:
                    handler(frame)
                except Exception:
                    pass
        return None  # no TCP reply needed for inbound pushes

    # ── discovery / bind ──────────────────────────────────────────────────────

    def find_capability(self, name: str = None) -> list[dict]:
        """Discover capability records from the network."""
        return self.swarm.discover(name)

    def bind_remote_to(self, target_node_id: str, capability_name: str, priority: int = 5) -> dict:
        """
        Bind a specific named capability on a specific provider node.
        Use when you already know the target_node_id from discovery.
        """
        with self.swarm._lock:
            provider_record = next(
                (dict(r) for r in self.swarm.records.values()
                 if r.get("node_id") == target_node_id and r.get("name") == capability_name),
                None,
            )

        request = {
            "type":            "bind_request",
            "from_node":       self.agent_id,
            "capability_name": capability_name,
            "needs":           self.needs,
            "priority":        priority,
        }

        response = self.swarm.send_and_recv(target_node_id, request, timeout=5.0)
        if not response:
            return {
                "status":           "error",
                "message":          f"No response from {target_node_id[:8]}",
                "provider_node_id": target_node_id,
            }

        verified = (
            response.get("status") in ("granted", "granted_by_preemption")
            and response.get("node_id") == target_node_id
            and response.get("verified_by_provider", False)
            and response.get("expires_at", 0) > time.time()
        )
        response["verified"]         = verified
        response["provider_node_id"] = target_node_id
        if provider_record:
            response.setdefault("device_class", provider_record.get("device_class", "unknown"))
        # Surface policy denials clearly for agent authors — no silent failure
        if response.get("status") == "denied":
            response["policy_message"] = response.get("message", "denied by device policy")
        return response

    def bind_remote(self, capability_name: str, priority: int = 5) -> dict:
        """
        Discover providers of `capability_name`, pick the first, and bind.
        Use bind_remote_to() when you want to target a specific provider.
        """
        now = time.time()
        with self.swarm._lock:
            cached = [
                dict(r) for r in self.swarm.records.values()
                if r.get("name") == capability_name
                and r.get("node_id") != self.agent_id
                and now - r.get("ts", 0) <= TTL
            ]

        providers = cached if cached else self.find_capability(capability_name)
        providers = [r for r in providers if r.get("node_id") != self.agent_id]

        if not providers:
            return {"status": "error", "message": f"No provider for '{capability_name}' found on network"}

        return self.bind_remote_to(providers[0]["node_id"], capability_name, priority)

    # ── data delivery: DEFAULT (on-demand pull) ────────────────────────────────

    def request_data(self, binding: dict, capability: str = None) -> dict:
        """
        THE PRIMARY METHOD for getting device data.

        Sends a get_reading request to the provider node, waits for one fresh
        structured frame, and returns it. This is the default/expected path:
        agent needs the data right now → device reads kernel signals at that
        instant and returns genuine fresh data. Zero background work on either end.

        Args:
            binding:    The binding dict returned by bind_remote / bind_remote_to.
            capability: Override capability name (defaults to binding's capability).

        Returns:
            {"type":"reading", "capability":..., "binding_id":..., "frame": {raw, derived, ts, seq}}
            or {"type":"error", "error": reason} if rejected.
        """
        cap    = capability or binding.get("capability_name", "")
        target = binding.get("provider_node_id", "")
        request = {
            "type":       "get_reading",
            "from_node":  self.agent_id,
            "binding_id": binding.get("binding_id", ""),
            "capability": cap,
        }
        response = self.swarm.send_and_recv(target, request, timeout=5.0)
        if not response:
            return {"type": "error", "error": "no_response", "binding_id": binding.get("binding_id")}
        # verify the response is for our binding
        if response.get("binding_id") != binding.get("binding_id"):
            return {"type": "error", "error": "binding_id_mismatch"}
        return response

    # ── data delivery: OPT-IN (streaming) ─────────────────────────────────────

    def start_stream(self, binding: dict, on_frame, hz: float = 5.0) -> None:
        """
        OPT-IN streaming. Ask the provider to start pushing frames at `hz` to
        this agent's TCP listener. on_frame(frame) is called for each arriving frame.

        Use this for monitoring over time. Prefer request_data() for one-shot needs.
        Frames stop immediately when stop_stream() is called.
        """
        cap    = binding.get("capability_name", "")
        target = binding.get("provider_node_id", "")
        bid    = binding.get("binding_id", "")

        # include our TCP address so the provider can push back to us
        ip, port = self.swarm.address
        request = {
            "type":          "subscribe",
            "from_node":     self.agent_id,
            "agent_address": [ip, port],
            "binding_id":    bid,
            "capability":    cap,
            "hz":            hz,
        }
        response = self.swarm.send_and_recv(target, request, timeout=5.0)
        if response and response.get("status") == "subscribed":
            sub_id = response.get("sub_id", "")
            self._stream_sub_ids[bid]  = sub_id
            self._stream_handlers[bid] = on_frame

    def stop_stream(self, binding: dict) -> None:
        """
        Stop opt-in streaming for this binding.
        Sends unsubscribe to the provider; clears local handler immediately.
        No more frames will be routed after this call returns.
        """
        bid    = binding.get("binding_id", "")
        target = binding.get("provider_node_id", "")

        # clear handlers first so any in-flight frames are silently dropped
        self._stream_handlers.pop(bid, None)
        self._stream_sub_ids.pop(bid, None)

        self.swarm.send_and_recv(target, {
            "type":       "unsubscribe",
            "from_node":  self.agent_id,
            "binding_id": bid,
        }, timeout=3.0)

    def release_binding(self, binding: dict) -> dict:
        """Release a binding on the provider. Called by ResourceHandle on exit."""
        cap_name = binding.get("capability_name", "")
        target   = binding.get("provider_node_id", "")
        response = self.swarm.send_and_recv(target, {
            "type":            "release_binding",
            "from_node":       self.agent_id,
            "capability_name": cap_name,
        }, timeout=3.0)
        return response or {"status": "ok"}
