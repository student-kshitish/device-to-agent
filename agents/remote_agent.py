import random
import threading
import time

from d2a import LANSwarm, SwarmTransport, PROTOCOL_VERSION, ProtocolVersionError, crypto
from d2a import signing

TTL = 30


class LeaseLostError(Exception):
    """
    Raised when a binding's lease can no longer be kept alive — the device denied
    a renewal, or the lease expired (renew never succeeded before the device-clock
    deadline, or a lease_expired push arrived). Surfaced on the next use of the
    binding (request_data / start_stream) so loss is never silent.
    """
    def __init__(self, binding_id: str, reason: str):
        self.binding_id = binding_id
        self.reason = reason
        super().__init__(f"lease lost for binding {binding_id[:8]}: {reason}")


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

    def __init__(self, name: str = "agent", transport: SwarmTransport = None,
                 auto_renew: bool = True):
        self.name = name
        # Persisted Ed25519 identity — agents are first-class signing principals
        # now. agent_id is DERIVED from the public key (can't be spoofed) and
        # stable across restarts. Keyed by name (share a name → share identity).
        self.keypair = crypto.load_or_create_keypair(name)
        self.agent_id = self.keypair.node_id
        self.private_key = self.keypair.private_key
        self.public_key = self.keypair.public_key
        # This agent pins the DEVICES it talks to. Per-name file.
        self.pins = crypto.PinStore(path=crypto.d2a_home() / f"pins-{name}.json")
        self.needs: list[str] = []
        self.swarm: SwarmTransport = transport if transport is not None else LANSwarm(node_id=self.agent_id)

        # streaming state: binding_id -> callback / device sub_id
        self._stream_handlers: dict[str, object]  = {}   # binding_id -> on_frame callable
        self._stream_sub_ids:  dict[str, str]      = {}   # binding_id -> device-side sub_id

        # lease state: binding_id -> lease dict (see _start_lease). auto_renew=False
        # reproduces the old "never renews" behavior — the binding then lives for
        # exactly one TTL and expires cleanly.
        self.auto_renew = auto_renew
        self._leases: dict[str, dict] = {}
        self._leases_lock = threading.Lock()
        self.on_lease_lost = None   # optional callback(binding_id, reason)

    def start(self) -> None:
        self.swarm.start()
        # route incoming stream_frame messages to registered handlers
        self.swarm.message_handler = self._on_message

    def stop(self) -> None:
        # Stop all auto-renew loops (simulates clean agent shutdown; a crashed
        # agent that never calls stop() simply stops renewing and its leases lapse).
        with self._leases_lock:
            leases = list(self._leases.values())
            self._leases.clear()
        for lease in leases:
            lease["stop"].set()
        self.swarm.stop()

    # ── incoming message router ────────────────────────────────────────────────

    def _on_message(self, message: dict) -> dict | None:
        """Handle messages pushed TO this agent (e.g. stream_frame from device)."""
        mtype = message.get("type")
        if mtype == "stream_frame":
            bid     = message.get("binding_id", "")
            frame   = message.get("frame", {})
            handler = self._stream_handlers.get(bid)
            if handler:
                try:
                    handler(frame)
                except Exception:
                    pass
        elif mtype == "lease_expired":
            # Best-effort device notification that our lease is gone. Stop renewing
            # and mark it lost so the next use raises LeaseLostError.
            bid = message.get("binding_id", "")
            with self._leases_lock:
                lease = self._leases.get(bid)
            if lease is not None:
                lease["stop"].set()
                self._mark_lost(lease, message.get("reason", "ttl_expired"))
        return None  # no TCP reply needed for inbound pushes

    def _check_version(self, response: dict) -> None:
        """
        Raise ProtocolVersionError if a response reports a version mismatch. Called
        at every point where a wire response is consumed, so a different-major peer
        surfaces as a clear typed exception naming both versions — never a silent
        failure or a confusing downstream error.
        """
        if isinstance(response, dict) and response.get("reason") == "version_mismatch":
            raise ProtocolVersionError(PROTOCOL_VERSION, response.get("peer_version"))

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

        # agent_address is our TCP listener — an UNVERIFIED hint the device may use
        # to push best-effort lease_expired notices back to us. Not authenticated.
        try:
            ip, port = self.swarm.address
            agent_address = [ip, port]
        except Exception:
            agent_address = None

        # Defense-in-depth: if we hold a signed record for this provider, verify
        # and TOFU-pin its device key now, so the bind_response's key must match.
        if provider_record and signing.is_signed(provider_record):
            signing.verify_record(provider_record, self.pins)

        # bind_request is a trust op — sign it (v + ts inside the signed payload;
        # agent_address now rides inside, tamper-evident).
        request = signing.sign_message({
            "type":            "bind_request",
            "from_node":       self.agent_id,
            "capability_name": capability_name,
            "needs":           self.needs,
            "priority":        priority,
            "agent_address":   agent_address,
        }, self.private_key, self.public_key)

        response = self.swarm.send_and_recv(target_node_id, request, timeout=5.0)
        if not response:
            return {
                "status":           "error",
                "message":          f"No response from {target_node_id[:8]}",
                "provider_node_id": target_node_id,
            }
        self._check_version(response)                     # raises on major mismatch

        # The device MUST have signed its response with a key that derives to the
        # node_id we dialed (and matches any prior pin). This rejects a MITM /
        # identity-claim forgery even if it copies status/node_id fields.
        trust_error = signing.verify_message(response, target_node_id, self.pins)
        response["provider_node_id"] = target_node_id
        if trust_error is not None:
            response["verified"]    = False
            response["trust_error"] = trust_error
            return response

        verified = (
            response.get("status") in ("granted", "granted_by_preemption")
            and response.get("node_id") == target_node_id
            and response.get("verified_by_provider", False)
            and response.get("expires_at", 0) > time.time()
        )
        response["verified"] = verified
        if provider_record:
            response.setdefault("device_class", provider_record.get("device_class", "unknown"))
        # Surface policy denials clearly for agent authors — no silent failure
        if response.get("status") == "denied":
            response["policy_message"] = response.get("message", "denied by device policy")
        # A verified bind starts a lease we keep alive (unless auto_renew is off).
        if verified:
            self._start_lease(response)
        return response

    # ── lease lifecycle (auto-renew) ───────────────────────────────────────────

    def _start_lease(self, binding: dict) -> None:
        """Record a lease for a freshly-verified binding and (if auto_renew) begin
        renewing it before the device-clock deadline."""
        bid = binding.get("binding_id", "")
        if not bid:
            return
        ttl = binding.get("lease_ttl") or 300
        exp = binding.get("lease_expires_at") or binding.get("expires_at") or (time.time() + ttl)
        lease = {
            "binding_id":       bid,
            "capability":       binding.get("capability_name", ""),
            "provider_node_id": binding.get("provider_node_id", ""),
            "lease_ttl":        ttl,
            "lease_expires_at": exp,
            "stop":             threading.Event(),
            "lost":             None,
        }
        with self._leases_lock:
            # replace any prior lease for this binding_id
            old = self._leases.get(bid)
            if old:
                old["stop"].set()
            self._leases[bid] = lease
        if self.auto_renew:
            t = threading.Thread(target=self._renew_loop, args=(lease,), daemon=True,
                                 name=f"renew-{bid[:8]}")
            lease["thread"] = t
            t.start()

    def _renew_loop(self, lease: dict) -> None:
        """
        Renew at TTL*(0.5±0.1). A single failed renew must NOT kill a healthy
        binding: on network failure/timeout we retry ~every TTL/10 (jittered) until
        success, an explicit denial, or the device-clock deadline actually passes.
        Only a denial or a real expiry marks the lease lost.
        """
        stop = lease["stop"]
        while not stop.is_set():
            ttl = lease["lease_ttl"]
            wait = ttl * (0.5 + random.uniform(-0.1, 0.1))
            if stop.wait(wait):
                return                                    # released / stopped cleanly

            # renew attempt with transient-failure retry
            while not stop.is_set():
                resp = self.swarm.send_and_recv(lease["provider_node_id"], signing.sign_message({
                    "type":            "renew_binding",
                    "from_node":       self.agent_id,
                    "binding_id":      lease["binding_id"],
                    "capability_name": lease["capability"],
                }, self.private_key, self.public_key), timeout=5.0)

                if resp is None:
                    # transient — don't give up unless the lease has truly expired
                    if time.time() >= lease["lease_expires_at"]:
                        self._mark_lost(lease, "ttl_expired")
                        return
                    retry = max(lease["lease_ttl"] / 10.0, 0.2) * (0.9 + random.uniform(0.0, 0.2))
                    if stop.wait(retry):
                        return
                    continue

                # A version mismatch is a hard, permanent failure — treat it like a
                # denial (stop renewing, surface loss), NEVER as a retryable drop.
                if resp.get("reason") == "version_mismatch":
                    self._mark_lost(lease, "version_mismatch")
                    return

                # The lease_renewed must be authentically from the pinned device.
                # An unsigned/forged/stale response is NOT trusted — treated like a
                # dropped packet (fail-safe): retry until success or real expiry, so
                # a MITM stripping signatures can only let the lease lapse.
                if signing.verify_message(resp, lease["provider_node_id"], self.pins) is not None:
                    if time.time() >= lease["lease_expires_at"]:
                        self._mark_lost(lease, "ttl_expired")
                        return
                    retry = max(lease["lease_ttl"] / 10.0, 0.2) * (0.9 + random.uniform(0.0, 0.2))
                    if stop.wait(retry):
                        return
                    continue

                if resp.get("status") == "renewed":
                    lease["lease_expires_at"] = resp.get("lease_expires_at", lease["lease_expires_at"])
                    lease["lease_ttl"]        = resp.get("lease_ttl", ttl)
                    break                                 # renewed — back to half-TTL sleep

                # explicit denial → lease is unrecoverable, surface immediately
                self._mark_lost(lease, resp.get("reason", "denied"))
                return

    def _mark_lost(self, lease: dict, reason: str) -> None:
        already = None
        with self._leases_lock:
            already = lease.get("lost")
            lease["lost"] = lease.get("lost") or reason
        if already:
            return
        print(f"[{self.name}] LEASE LOST binding={lease['binding_id'][:8]} reason={reason}")
        cb = self.on_lease_lost
        if cb:
            try:
                cb(lease["binding_id"], reason)
            except Exception:
                pass

    def _raise_if_lost(self, binding_id: str) -> None:
        with self._leases_lock:
            lease = self._leases.get(binding_id)
            reason = lease.get("lost") if lease else None
        if reason:
            raise LeaseLostError(binding_id, reason)

    def _stop_lease(self, binding_id: str) -> None:
        with self._leases_lock:
            lease = self._leases.pop(binding_id, None)
        if lease:
            lease["stop"].set()

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
        self._raise_if_lost(binding.get("binding_id", ""))
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
        self._check_version(response)                     # raises on major mismatch
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
        self._raise_if_lost(binding.get("binding_id", ""))
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
        self._check_version(response)                     # raises on major mismatch
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
        self._stop_lease(binding.get("binding_id", ""))   # stop auto-renew first
        cap_name = binding.get("capability_name", "")
        target   = binding.get("provider_node_id", "")
        response = self.swarm.send_and_recv(target, signing.sign_message({
            "type":            "release_binding",
            "from_node":       self.agent_id,
            "capability_name": cap_name,
        }, self.private_key, self.public_key), timeout=3.0)
        return response or {"status": "ok"}
