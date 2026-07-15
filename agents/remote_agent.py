import random
import threading
import time

from d2a import LANSwarm, SwarmTransport, PROTOCOL_VERSION, ProtocolVersionError, crypto
from d2a import signing
from d2a import errors

TTL = 30


class LeaseLostError(Exception):
    """
    Raised when a binding's lease can no longer be kept alive — the device denied
    a renewal, the lease expired (renew never succeeded before the device-clock
    deadline, or a lease_expired push arrived), or the device announced a graceful
    shutdown. Surfaced on the next use of the binding (request_data / start_stream)
    so loss is never silent.

    `.code` is the registry code driving the loss (errors.LEASE_EXPIRED,
    errors.DEVICE_SHUTDOWN, errors.VERSION_MISMATCH, a trust code, …). The harness
    branches on it: DEVICE_SHUTDOWN = announced departure (don't retry soon);
    LEASE_EXPIRED after a silent vanish = backoff rediscovery. `.reason` is kept as
    an alias of `.code`.
    """
    def __init__(self, binding_id: str, code: str):
        self.binding_id = binding_id
        self.code = code
        self.reason = code            # alias — same value, older name
        super().__init__(f"lease lost for binding {binding_id[:8]}: {code}")


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

        # event state: event_sub_id -> {cb, binding_id, last_seq}. Keyed by the
        # device-assigned event_sub_id so many conditions can coexist per binding.
        self._event_handlers: dict[str, dict] = {}
        # async task state: task_id -> {cb, binding_id}. Completion arrives as a
        # kind:"task" event on the same channel and fires the callback once.
        self._task_handlers: dict[str, dict] = {}

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
        elif mtype == "event" and message.get("kind") == "task":
            # Async task completion (subscription implicit with the task). Fires
            # the registered on_complete once, then drops the handler (terminal).
            tid = message.get("task_id", "")
            h   = self._task_handlers.pop(tid, None)
            if h:
                try:
                    h["cb"](message)
                except Exception:
                    pass
        elif mtype == "event":
            # Conditional event pushed from the device (fire-and-forget, unsigned
            # data-path, same class as stream_frame). Route to the handler by
            # event_sub_id and surface any sequence gap (no delivery guarantee —
            # an agent needing certainty re-reads on receipt).
            esid = message.get("event_sub_id", "")
            h    = self._event_handlers.get(esid)
            if h:
                seq  = message.get("seq", 0)
                last = h.get("last_seq", 0)
                if isinstance(seq, int) and seq > last + 1:
                    message["_gap"] = seq - last - 1   # frames missed since last
                h["last_seq"] = seq
                try:
                    h["cb"](message)
                except Exception:
                    pass
        elif mtype == "lease_expired":
            # Best-effort device notification that our lease lapsed (silent-vanish
            # class). Stop renewing and mark it lost so the next use raises.
            self._on_binding_death(message.get("binding_id", ""),
                                   message.get("code", errors.LEASE_EXPIRED))
        elif mtype == "device_shutdown":
            # The device ANNOUNCED a graceful departure — distinct from a lapsed
            # lease. Same local teardown, but the loss code is device_shutdown so a
            # harness can branch: announced shutdown = don't retry this device soon;
            # a silent vanish (lease_expired / renew timeout) = backoff rediscovery.
            self._on_binding_death(message.get("binding_id", ""),
                                   message.get("code", errors.DEVICE_SHUTDOWN))
        return None  # no TCP reply needed for inbound pushes

    def _on_binding_death(self, bid: str, code: str) -> None:
        """Shared teardown for a device-pushed binding-death notice (lease_expired
        or device_shutdown): stop renewing, mark the lease lost with `code`, and
        drop the binding's event + task handlers."""
        with self._leases_lock:
            lease = self._leases.get(bid)
        if lease is not None:
            lease["stop"].set()
            self._mark_lost(lease, code)
        self._event_handlers = {
            k: v for k, v in self._event_handlers.items()
            if v.get("binding_id") != bid
        }
        self._task_handlers = {
            k: v for k, v in self._task_handlers.items()
            if v.get("binding_id") != bid
        }

    def _check_version(self, response: dict) -> None:
        """
        Raise ProtocolVersionError if a response reports a version mismatch. Called
        at every point where a wire response is consumed, so a different-major peer
        surfaces as a clear typed exception naming both versions — never a silent
        failure or a confusing downstream error.
        """
        if isinstance(response, dict) and response.get("code") == errors.VERSION_MISMATCH:
            raise ProtocolVersionError(PROTOCOL_VERSION, response.get("peer_version"))

    # ── discovery / bind ──────────────────────────────────────────────────────

    def find_capability(self, name: str = None) -> list[dict]:
        """Discover capability records from the network. Records may carry a
        signed `manifest` (v1.2) describing the capability — see describe()."""
        return self.swarm.discover(name)

    def describe(self, capability_name: str, node_id: str = None) -> dict | None:
        """
        Return the parsed capability manifest for `capability_name` from the
        discovery cache (the machine-readable self-description: reading schema,
        actions, consent tier, streaming), or None if the record has no manifest
        / is unknown. Pass node_id to disambiguate multiple providers.

        The manifest was signed inside the capability record, so if the record
        passed verification the manifest is authentic. Call find_capability()
        first if the cache is cold.
        """
        with self.swarm._lock:
            for (nid, name), rec in self.swarm.records.items():
                if name == capability_name and (node_id is None or nid == node_id):
                    man = rec.get("manifest")
                    if man is not None:
                        return man
        return None

    def describe_node(self, target_node_id: str) -> dict:
        """
        Ask a reachable node for its FULL, consent-filtered capability catalog +
        node self-descriptor (v1.8 — the MCP list_tools / A2A agent-card
        equivalent). Point-to-point, signed both ways: our request is signed (so
        the node can raise our disclosure above open tier if we're authorized),
        the response is host-key-signed and verified + TOFU-pinned here exactly
        like a bind_response. Returns {node, catalog, verified} or an error dict.

        The catalog omits any capability we could not bind right now (deny-by-
        default): sensitive / intervention entries are ABSENT — not name-only —
        unless the owner has pre-allowed them. Composes with describe(name): use
        this for the whole node, describe() for one cached capability's manifest.
        """
        request = signing.sign_message(
            {"type": "describe_node", "from_node": self.agent_id},
            self.private_key, self.public_key,
        )
        response = self.swarm.send_and_recv(target_node_id, request, timeout=5.0)
        if not response:
            return {"status": "error", "code": errors.NO_RESPONSE,
                    "detail": f"No response from {target_node_id[:8]}",
                    "provider_node_id": target_node_id}
        self._check_version(response)                      # raises on major mismatch

        trust_error = signing.verify_message(response, target_node_id, self.pins)
        response["provider_node_id"] = target_node_id
        if trust_error is not None:
            response["verified"]    = False
            response["trust_error"] = trust_error
            return response
        response["verified"] = True
        return response

    def node_capabilities(self, node_id: str) -> list[str]:
        """
        Enumerate the OPEN-TIER capability NAMES a node offers with ZERO prior name
        knowledge (v1.8) — "what does node X offer", complementing find_capability
        ("who offers Y"). On a DHT this reads the signed node:<id> descriptor and
        verifies + TOFU-pins it before trusting the names; a tampered / key-changed
        descriptor yields []. On a broadcast transport (no keyed descriptor) it
        falls back to the names already in the local discovery cache. For the FULL
        manifests, follow up with describe_node(node_id).
        """
        desc = self.swarm.fetch_node_descriptor(node_id)
        if desc is not None:
            if signing.verify_record(desc, self.pins) is None:
                return sorted(desc.get("capability_names", []))
            return []                                      # tamper / pin mismatch
        # Fallback: names from whatever records we've cached for this node.
        with self.swarm._lock:
            return sorted({name for (nid, name) in self.swarm.records
                           if nid == node_id and name})

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
                "code":             errors.NO_RESPONSE,
                "detail":           f"No response from {target_node_id[:8]}",
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
            response["policy_message"] = response.get("detail", "denied by device policy")
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
                        self._mark_lost(lease, errors.LEASE_EXPIRED)
                        return
                    retry = max(lease["lease_ttl"] / 10.0, 0.2) * (0.9 + random.uniform(0.0, 0.2))
                    if stop.wait(retry):
                        return
                    continue

                # A version mismatch is a hard, permanent failure — treat it like a
                # denial (stop renewing, surface loss), NEVER as a retryable drop.
                if resp.get("code") == errors.VERSION_MISMATCH:
                    self._mark_lost(lease, errors.VERSION_MISMATCH)
                    return

                # The lease_renewed must be authentically from the pinned device.
                # An unsigned/forged/stale response is NOT trusted — treated like a
                # dropped packet (fail-safe): retry until success or real expiry, so
                # a MITM stripping signatures can only let the lease lapse.
                if signing.verify_message(resp, lease["provider_node_id"], self.pins) is not None:
                    if time.time() >= lease["lease_expires_at"]:
                        self._mark_lost(lease, errors.LEASE_EXPIRED)
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
                self._mark_lost(lease, resp.get("code", "denied"))
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
            return {"status": "error", "code": errors.NO_PROVIDER,
                    "detail": f"No provider for '{capability_name}' found on network"}

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
            or the unified {"type":"error", "code": <errors.*>, ...} if rejected.
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
            return errors.error(errors.NO_RESPONSE, binding_id=binding.get("binding_id"))
        self._check_version(response)                     # raises on major mismatch
        # verify the response is for our binding
        if response.get("binding_id") != binding.get("binding_id"):
            return errors.error(errors.BINDING_ID_MISMATCH,
                                binding_id=binding.get("binding_id"))
        return response

    def call_action(self, binding: dict, action: str, params: dict = None,
                    capability: str = None, on_complete=None) -> dict:
        """
        Invoke a virtual capability's action (Guardian VSO / Synthesis emergent)
        declared in its manifest — e.g. describe()['actions']. Same binding-scope
        gate as request_data (a data-path op; the token/lease already authorized
        it). Returns {"type":"action_result", ..., "result": {...}} or an error.

        LONG-RUNNING actions (manifest actions.<name>.long_running) return
        immediately with result == {"task_id", "status":"running"} — the call does
        NOT block for the work. Completion arrives later as a kind:"task" event;
        pass on_complete(event) to be called when it does, or poll task_status().
        """
        self._raise_if_lost(binding.get("binding_id", ""))
        cap    = capability or binding.get("capability_name", "")
        target = binding.get("provider_node_id", "")
        bid    = binding.get("binding_id", "")
        request = {
            "type":       "action",
            "from_node":  self.agent_id,
            "binding_id": bid,
            "capability": cap,
            "action":     action,
            "params":     params or {},
        }
        response = self.swarm.send_and_recv(target, request, timeout=5.0)
        if not response:
            return errors.error(errors.NO_RESPONSE, binding_id=bid)
        self._check_version(response)
        # register the completion callback if this returned a running task
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(result, dict) and result.get("task_id") and result.get("status") == "running":
            if on_complete is not None:
                self._task_handlers[result["task_id"]] = {"cb": on_complete, "binding_id": bid}
        return response

    def propose_intervention(self, binding: dict, plan: dict,
                             capability: str = None,
                             owner_approval: dict = None) -> dict:
        """
        Propose a MUTATING intervention plan (Phase 8). The device runs a DOUBLE
        GATE — this binding already needed owner approval to exist (the right to
        PROPOSE), and now this specific plan needs its own per-plan owner approval
        before anything executes. On approval the DEVICE executes the fix and runs
        the plan's declared VERIFY itself (a diagnostic condition that must hold
        after) — the verify result is never trusted from the agent.

        `plan` is an InterventionPlan: {action, params, evidence, expected,
        verify:{diagnostic, condition}, reversible, reversible_how|reversible_ack}.

        REMOTE KEYED APPROVAL (Phase 10A): if the device has an owner key
        registered and no local console callback, a first proposal returns
        status=="pending_owner_approval" with an `owner_approval_request`
        ({plan_hash, device_node_id, nonce, ts}). The owner signs it
        (signing.sign_owner_approval) and the caller resubmits this SAME plan with
        the resulting dict as `owner_approval`. The device verifies the signature
        against the pinned owner key and proceeds. See examples/keyed_approval.

        Returns {"type":"intervention_result", "status": executed|denied|
        failed_verify|refused_preflight|error|pending_owner_approval, "approved",
        "executed", "verify", "reversible", "reversible_how", "plan_hash",
        "audit_seq", ...} or the unified error shape. NOTE: a fix that ran but whose
        verify failed comes back status=="failed_verify" (never a silent success).
        """
        self._raise_if_lost(binding.get("binding_id", ""))
        cap    = capability or binding.get("capability_name", "")
        target = binding.get("provider_node_id", "")
        bid    = binding.get("binding_id", "")
        req = {
            "type":       "propose_intervention",
            "from_node":  self.agent_id,
            "binding_id": bid,
            "capability": cap,
            "plan":       plan,
        }
        if owner_approval is not None:
            req["owner_approval"] = owner_approval
        response = self.swarm.send_and_recv(target, req, timeout=30.0)   # mutating subprocess + verify read
        if not response:
            return errors.error(errors.NO_RESPONSE, binding_id=bid)
        self._check_version(response)
        return response

    def task_status(self, binding: dict, task_id: str) -> dict:
        """Poll a long-running task. Returns {"status": running|done|failed|
        cancelled|unknown, "result"?, "error_detail"?}. "unknown" once the task's
        lease dies (record dropped) or the id is not this binding's."""
        target = binding.get("provider_node_id", "")
        response = self.swarm.send_and_recv(target, {
            "type":       "task_status",
            "from_node":  self.agent_id,
            "binding_id": binding.get("binding_id", ""),
            "task_id":    task_id,
        }, timeout=5.0)
        if not response:
            return errors.error(errors.NO_RESPONSE, task_id=task_id)
        self._check_version(response)
        return response

    # ── lease delegation (Phase 10B) ──────────────────────────────────────────

    def delegate_binding(self, binding: dict, delegate_agent_id: str,
                         scope: dict = None, sub_ttl: float = None,
                         owner_approval: dict = None) -> dict:
        """
        Delegate a binding this agent (A) holds to agent B. The device issues B a
        CHILD binding capped by A's remaining lease (or `sub_ttl`, whichever is
        shorter), RE-GATED for B (open passes; sensitive re-checks B; intervention
        requires a keyed owner approval naming B — pass it via `owner_approval`,
        e.g. signing.sign_delegation_approval(...)), optionally narrowed to a subset
        of actions via `scope={"actions":[...]}` (never wider). Delegation transfers
        USE, never the authority to approve. Signed by A.

        Returns the device's delegation_result (status "delegated" with the child
        binding_id, or "denied" with a code). Use accept_delegation() on B's side to
        turn a successful result into a usable binding dict.
        """
        self._raise_if_lost(binding.get("binding_id", ""))
        target = binding.get("provider_node_id", "")
        try:
            ip, port = self.swarm.address
            deleg_addr = [ip, port]
        except Exception:
            deleg_addr = None
        req = {
            "type":              "delegate_binding",
            "from_node":         self.agent_id,
            "parent_binding_id": binding.get("binding_id", ""),
            "capability":        binding.get("capability_name", ""),
            "delegate_agent_id": delegate_agent_id,
        }
        if scope is not None:
            req["scope"] = scope
        if sub_ttl is not None:
            req["sub_ttl"] = sub_ttl
        if owner_approval is not None:
            req["owner_approval"] = owner_approval
        response = self.swarm.send_and_recv(
            target, signing.sign_message(req, self.private_key, self.public_key), timeout=10.0)
        if not response:
            return errors.error(errors.NO_RESPONSE, binding_id=binding.get("binding_id", ""))
        self._check_version(response)
        if signing.verify_message(response, target, self.pins) is not None:
            response["verified"] = False
        return response

    def revoke_delegation(self, provider_node_id: str, child_binding_id: str) -> dict:
        """Revoke a delegation this agent granted — cuts the delegate off now
        (device tears the child down through the unified path). Signed."""
        req = {"type": "revoke_delegation", "from_node": self.agent_id,
               "binding_id": child_binding_id}
        response = self.swarm.send_and_recv(
            provider_node_id, signing.sign_message(req, self.private_key, self.public_key), timeout=5.0)
        if not response:
            return errors.error(errors.NO_RESPONSE, binding_id=child_binding_id)
        self._check_version(response)
        return response

    @staticmethod
    def accept_delegation(delegation_result: dict) -> dict:
        """B-side: turn a successful delegation_result into a binding dict usable
        with request_data / start_stream / propose_intervention. A delegated child
        is NON-RENEWABLE (its right rides A's lease), so no auto-renew lease is
        started — it simply works until A's lease ends, A revokes, or it lapses."""
        return {
            "binding_id":       delegation_result.get("binding_id", ""),
            "provider_node_id": delegation_result.get("node_id", ""),
            "capability_name":  delegation_result.get("capability", ""),
            "delegated":        True,
        }

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

    # ── conditional events: OPT-IN (v1.3) ─────────────────────────────────────

    def on_event(self, binding: dict, condition: dict, callback,
                 eval_hz: float = 5.0, capability: str = None) -> dict:
        """
        OPT-IN conditional events. Ask the provider to notify this agent when
        `condition` fires on the capability's live reading.

        `condition` is ONE manifest reading field + operator:
            {"field": <manifest field>, "op": gt|lt|ge|le|eq|ne|changed,
             "value": <scalar; omit for "changed">}
        Validated device-side against the capability manifest — an unknown field
        or op/type mismatch comes back as the unified error with
        code == errors.INVALID_CONDITION.

        Events fire on EDGE (the crossing), not every sample above threshold, and
        re-arm when the condition becomes false again. callback(event) runs per
        delivered event; event carries the triggering reading snapshot and a
        per-subscription monotonic "seq" (a jump means frames were missed —
        delivery is best-effort, so re-read if you need certainty).

        Returns the device response (contains "event_sub_id" on success).
        """
        self._raise_if_lost(binding.get("binding_id", ""))
        cap    = capability or binding.get("capability_name", "")
        target = binding.get("provider_node_id", "")
        bid    = binding.get("binding_id", "")

        ip, port = self.swarm.address
        request = {
            "type":          "subscribe_event",
            "from_node":     self.agent_id,
            "agent_address": [ip, port],
            "binding_id":    bid,
            "capability":    cap,
            "condition":     condition,
            "eval_hz":       eval_hz,
        }
        response = self.swarm.send_and_recv(target, request, timeout=5.0)
        if not response:
            return errors.error(errors.NO_RESPONSE, binding_id=bid)
        self._check_version(response)
        if response.get("status") == "subscribed":
            esid = response.get("event_sub_id", "")
            self._event_handlers[esid] = {"cb": callback, "binding_id": bid, "last_seq": 0}
        return response

    def off_event(self, binding: dict, event_sub_id: str) -> dict:
        """Cancel one conditional-event subscription. Clears the local handler
        immediately, then tells the provider to stop evaluating it."""
        self._event_handlers.pop(event_sub_id, None)
        target = binding.get("provider_node_id", "")
        response = self.swarm.send_and_recv(target, {
            "type":         "unsubscribe_event",
            "from_node":    self.agent_id,
            "binding_id":   binding.get("binding_id", ""),
            "event_sub_id": event_sub_id,
        }, timeout=3.0)
        return response or {"status": "ok"}

    def release_binding(self, binding: dict) -> dict:
        """Release a binding on the provider. Called by ResourceHandle on exit."""
        bid = binding.get("binding_id", "")
        # drop any event + task handlers for this binding (device tears down its
        # side through the unified lease/stream cleanup path on release)
        self._event_handlers = {
            k: v for k, v in self._event_handlers.items() if v.get("binding_id") != bid
        }
        self._task_handlers = {
            k: v for k, v in self._task_handlers.items() if v.get("binding_id") != bid
        }
        self._stop_lease(binding.get("binding_id", ""))   # stop auto-renew first
        cap_name = binding.get("capability_name", "")
        target   = binding.get("provider_node_id", "")
        response = self.swarm.send_and_recv(target, signing.sign_message({
            "type":            "release_binding",
            "from_node":       self.agent_id,
            "capability_name": cap_name,
        }, self.private_key, self.public_key), timeout=3.0)
        return response or {"status": "ok"}
