import os
import threading
import time

from d2a import (
    Capability, BindToken,
    verify_bind_token, CapabilityBroker,
    rebind, renew,
    probe_all,
    LANSwarm, SwarmTransport,
    IOContract, CapabilityContract,
    crypto,
)
from d2a import signing
from d2a.resource_probes import probe_resources, RESOURCE_SENSITIVITY
from d2a.policy import ResourcePolicy
from d2a.stream_source import (
    CPUSource, MemorySource, GPUSource,
    ThermalSource, BatterySource, DiskIOSource, NetIOSource,
    CameraMetaSource, MicrophoneMetaSource, LocationMetaSource,
    DisplayMetaSource, StorageSource, NetworkMetaSource,
)
from d2a.data_provider import DataProvider
from d2a.sense_layer import SenseLayer
from d2a.sense_types import SenseRequest, SenseFrame


class DeviceRuntime:
    def __init__(
        self,
        name: str = "node",
        transport: SwarmTransport = None,
        capability_override: list[str] | None = None,
        # Owner-consent policy params — default (nothing passed) = safe
        open_resources:      list[str] | None = None,
        deny_resources:      list[str] | None = None,
        approval_callback                      = None,
        # Binding lease TTL in seconds. The device clock is the sole authority for
        # expiry; agents renew before this elapses (see RemoteAgent auto-renew).
        lease_ttl:           int               = 300,
    ):
        self.name = name
        self.lease_ttl = int(lease_ttl)
        # Persisted Ed25519 identity. node_id is DERIVED from the public key
        # (crypto.derive_node_id), so this node cannot claim an id it doesn't
        # hold the key for. Keyed by `name` → stable across restarts, which is
        # what makes peers' TOFU pins hold. NOTE: two runtimes sharing a name
        # (and D2A_HOME) share one identity — name your nodes uniquely.
        self.keypair = crypto.load_or_create_keypair(name)
        self.node_id = self.keypair.node_id
        self.private_key = self.keypair.private_key
        self.public_key = self.keypair.public_key
        # This device pins the AGENTS that bind to it. Per-name pin file so
        # concurrent nodes in one process don't clobber each other's pins.
        self.pins = crypto.PinStore(path=crypto.d2a_home() / f"pins-{name}.json")

        # ── hardware + resource probes ────────────────────────────────────────
        self.snapshot          = probe_all()
        self.resource_snapshot = probe_resources()
        self.device_class      = self.snapshot["device_class"]
        self.power_state       = self.snapshot.get("battery")
        self.capabilities: dict[str, Capability] = self._build_capabilities(
            self.snapshot, self.resource_snapshot
        )

        # ── capability contracts (for composition planner) ────────────────────
        self.capability_contracts: dict[str, CapabilityContract] = \
            self._build_capability_contracts(self.snapshot, self.resource_snapshot)

        # ── owner-consent policy (safe defaults, no args needed) ──────────────
        self.policy = ResourcePolicy(device_class=self.device_class)
        if open_resources:
            for r in open_resources:
                self.policy.allow(r)
        if deny_resources:
            for r in deny_resources:
                self.policy.deny(r)
        if approval_callback is not None:
            self.policy.set_approval_callback(approval_callback)

        # ── broker ────────────────────────────────────────────────────────────
        self.broker = CapabilityBroker(self)
        self.broker.quotas = {n: 1 for n in self.capabilities}

        self.swarm: SwarmTransport = transport if transport is not None else LANSwarm(node_id=self.node_id)

        # ── data delivery layer ───────────────────────────────────────────────
        # Nothing runs until an agent requests data.
        # NOTE: build sources BEFORE applying override so the DataProvider exists
        # before _apply_override may inject extra sources into it.
        self.data = DataProvider(self._build_sources(self.snapshot, self.resource_snapshot))

        # binding_id -> data_provider sub_id (stream cleanup on release/preemption)
        self._binding_subs: dict[str, str] = {}

        # ── sense layer ───────────────────────────────────────────────────────
        # Nothing runs until handle() is called. Shares the same sources dict as
        # DataProvider so no duplicate source instances are created.
        self.sense = SenseLayer(self.data._sources, self.device_class)

        # ── peripheral registry (Case 2 generalisation) ───────────────────────
        # Tracks peripherals registered at runtime via attach_peripheral().
        # Only paths the user/config explicitly provides — no auto-scan of /dev.
        self._peripheral_paths: dict[str, str] = {}   # realpath → cap_name

        # Now apply override (may inject sources into self.data)
        if capability_override is not None:
            self.capabilities = self._apply_override(capability_override)
            self.broker.quotas = {n: 1 for n in self.capabilities}

        print(f"[{self.name}] class={self.device_class}  node={self.node_id[:8]}  "
              f"offering={list(self.capabilities)}")

    # ── capability building ────────────────────────────────────────────────────

    def _build_capabilities(
        self, snapshot: dict, resource_snapshot: dict = None
    ) -> dict[str, Capability]:
        caps: dict[str, Capability] = {}
        battery_tags  = ["battery_aware"] if "battery" in snapshot else []
        rs            = resource_snapshot or {}

        # ── existing hardware capabilities ────────────────────────────────────
        compute_state: dict = {
            "cpu_count": snapshot["cpu"]["count"],
            "arch":      snapshot["cpu"]["arch"],
        }
        if "loadavg" in snapshot:
            compute_state["load1"] = snapshot["loadavg"]["load1"]
        if "memory" in snapshot:
            m = snapshot["memory"]
            compute_state["mem_total_mb"]     = m["total_mb"]
            compute_state["mem_available_mb"] = m["available_mb"]
            compute_state["mem_used_percent"] = m["used_percent"]
        if "disk" in snapshot:
            d = snapshot["disk"]
            compute_state["disk_free_gb"]  = d["free_gb"]
            compute_state["disk_used_pct"] = d["used_pct"]
        caps["compute"] = Capability(
            name="compute",
            tags=["compute", "open"] + battery_tags,
            live_state=compute_state,
            node_id=self.node_id,
            public_key=self.public_key,
        )

        if "gpu" in snapshot:
            caps["gpu"] = Capability(
                name="gpu",
                tags=["compute", "gpu", "open"] + battery_tags,
                live_state=snapshot["gpu"],
                node_id=self.node_id,
                public_key=self.public_key,
            )

        if "thermal" in snapshot or "sensors" in snapshot:
            sense_state: dict = {}
            if "thermal" in snapshot:
                sense_state["thermal_zones"]  = snapshot["thermal"]["zone_count"]
                sense_state["sample_temps_c"] = snapshot["thermal"]["temps_c"][:6]
            if "sensors" in snapshot:
                sense_state["sensor_inputs"] = snapshot["sensors"]["count"]
                sense_state["hwmons"]        = snapshot["sensors"]["hwmons"]
            caps["sensing"] = Capability(
                name="sensing",
                tags=["sensing", "thermal", "open"] + battery_tags,
                live_state=sense_state,
                node_id=self.node_id,
                public_key=self.public_key,
            )

        if "battery" in snapshot:
            caps["battery_aware"] = Capability(
                name="battery_aware",
                tags=["battery_aware", "battery", "open"],
                live_state=snapshot["battery"],
                node_id=self.node_id,
                public_key=self.public_key,
            )

        # ── generic resource capabilities ─────────────────────────────────────
        # Each resource carries access level in its live_state and tags.
        for res_name, res_data in rs.items():
            sensitivity = RESOURCE_SENSITIVITY.get(res_name, "sensitive")
            access_tag  = "open" if sensitivity == "open" else "owner_consent"
            caps[res_name] = Capability(
                name=res_name,
                tags=[res_name, access_tag] + (battery_tags if sensitivity == "open" else []),
                live_state=dict(res_data),
                node_id=self.node_id,
                public_key=self.public_key,
            )

        return caps

    def _build_capability_contracts(
        self, snapshot: dict, resource_snapshot: dict = None
    ) -> dict[str, CapabilityContract]:
        """
        Attach CapabilityContract to each hardware capability where meaningful.
        Unknown format → "unknown" so the contract checker fails explicitly, never silently passes.
        Shape/rate values are best-effort from probe data; None = not known.
        """
        contracts: dict[str, CapabilityContract] = {}
        rs = resource_snapshot or {}

        # compute → can host a model consumer accepting float32 tensors
        # (shape is configurable; default matches common vision model input)
        contracts["compute"] = CapabilityContract(
            name="compute",
            role="consumer",
            accepts=IOContract(
                media="tensor", format="float32",
                shape=(640, 480, 3),   # TODO: configurable per model
                rate=None,
            ),
        )

        if "gpu" in snapshot:
            contracts["gpu"] = CapabilityContract(
                name="gpu",
                role="consumer",
                accepts=IOContract(
                    media="tensor", format="float32",
                    shape=(640, 480, 3),   # TODO: configurable per model
                    rate=None,
                ),
            )

        # camera → producer; format detected from probe, "unknown" if not determinable
        if "camera" in rs:
            cam = rs["camera"]
            fmt = cam.get("format", "unknown")
            w   = cam.get("width")
            h   = cam.get("height")
            fps = cam.get("fps")
            shape = (w, h, 3) if (w and h) else None
            contracts["camera"] = CapabilityContract(
                name="camera",
                role="producer",
                produces=IOContract(
                    media="image", format=fmt,
                    shape=shape, rate=fps,
                ),
            )

        # microphone → producer; pcm16 is the standard capture format
        if "microphone" in rs:
            contracts["microphone"] = CapabilityContract(
                name="microphone",
                role="producer",
                produces=IOContract(media="audio", format="pcm16", shape=None, rate=None),
            )

        # sensing → producer of scalar values (temperature, etc.)
        if "thermal" in snapshot or "sensors" in snapshot:
            contracts["sensing"] = CapabilityContract(
                name="sensing",
                role="producer",
                produces=IOContract(media="scalar", format="float32", shape=None, rate=None),
            )

        return contracts

    def _build_sources(self, snapshot: dict, resource_snapshot: dict = None) -> dict[str, list]:
        """Map capability names to fresh-read signal sources for DataProvider."""
        sources: dict[str, list] = {}
        rs = resource_snapshot or {}

        # compute: CPU + memory + optional disk/net IO rates
        compute_srcs = [CPUSource(), MemorySource()]
        if os.path.exists("/proc/diskstats"):
            compute_srcs.append(DiskIOSource())
        if os.path.exists("/proc/net/dev"):
            compute_srcs.append(NetIOSource())
        sources["compute"] = compute_srcs

        if "gpu" in snapshot:
            sources["gpu"] = [GPUSource()]

        if "thermal" in snapshot or "sensors" in snapshot:
            sources["sensing"] = [ThermalSource()]

        if "battery" in snapshot:
            sources["battery_aware"] = [BatterySource()]

        # generic resources — always register metadata sources when capability exists
        if "camera" in rs:
            sources["camera"] = [CameraMetaSource()]
        if "microphone" in rs:
            sources["microphone"] = [MicrophoneMetaSource()]
        if "location" in rs:
            sources["location"] = [LocationMetaSource()]
        if "storage" in rs:
            sources["storage"] = [StorageSource()]
        if "network" in rs:
            sources["network"] = [NetworkMetaSource(), NetIOSource()]
        if "display" in rs:
            sources["display"] = [DisplayMetaSource()]

        return sources

    def _apply_override(self, cap_names: list[str]) -> dict[str, Capability]:
        """Demo-only: replace capability set with a named list. Real caps kept where matched."""
        battery_tags = ["battery_aware"] if "battery" in self.snapshot else []
        caps: dict[str, Capability] = {}
        for cap_name in cap_names:
            if cap_name in self.capabilities:
                caps[cap_name] = self.capabilities[cap_name]
            else:
                sensitivity = RESOURCE_SENSITIVITY.get(cap_name, "sensitive")
                access_tag  = "open" if sensitivity == "open" else "owner_consent"
                caps[cap_name] = Capability(
                    name=cap_name,
                    tags=[cap_name, access_tag],
                    live_state={"simulated": True, "access": access_tag},
                    node_id=self.node_id,
                    public_key=self.public_key,
                )
                # always register a metadata source for overridden capabilities
                # so get_reading returns a safe frame even without real hardware
                _src_map = {
                    "camera":     CameraMetaSource,
                    "microphone": MicrophoneMetaSource,
                    "location":   LocationMetaSource,
                    "display":    DisplayMetaSource,
                    "storage":    StorageSource,
                    "network":    NetworkMetaSource,
                }
                if cap_name in _src_map:
                    self.data._sources[cap_name] = [_src_map[cap_name]()]
        return caps

    # ── capability interface ───────────────────────────────────────────────────

    def advertise(self) -> list[Capability]:
        return list(self.capabilities.values())

    def get_capability(self, name: str) -> Capability | None:
        return self.capabilities.get(name)

    def refresh_hardware(self) -> dict:
        self.snapshot          = probe_all()
        self.resource_snapshot = probe_resources()
        self.device_class      = self.snapshot["device_class"]
        self.power_state       = self.snapshot.get("battery")
        self.capabilities      = self._build_capabilities(self.snapshot, self.resource_snapshot)
        return self.snapshot

    def live_capabilities(self) -> list[Capability]:
        self.refresh_hardware()
        return self.advertise()

    # ── swarm integration ──────────────────────────────────────────────────────

    def start_swarm(self) -> None:
        self.swarm.start()
        self.swarm.message_handler = self._on_message
        self.publish_capabilities()
        self._start_lease_sweeper()

    def stop_swarm(self) -> None:
        self._sweeper_running = False
        self.swarm.stop()

    # ── lease expiry sweeper ────────────────────────────────────────────────────

    def _start_lease_sweeper(self) -> None:
        """
        Background thread that reaps expired leases. Interval is min(TTL/10, 5s)
        so a lapsed lease is freed within a small fraction of its TTL. All teardown
        goes through broker.sweep_expired() → the shared release path (frees the
        slot, fires the waitqueue auto-grant); we then kill the binding's stream and
        send a best-effort lease_expired push. The device clock alone decides expiry.
        """
        self._sweeper_running = True
        interval = min(max(self.lease_ttl / 10.0, 0.2), 5.0)

        def _loop():
            while self._sweeper_running:
                time.sleep(interval)
                if not self._sweeper_running:
                    break
                try:
                    self._sweep_leases_once()
                except Exception:
                    pass

        threading.Thread(target=_loop, daemon=True, name=f"lease-sweeper-{self.name}").start()

    def _sweep_leases_once(self) -> list[dict]:
        """One sweep pass. Returns the list of expired-binding infos (for tests)."""
        expired = self.broker.sweep_expired()
        for info in expired:
            binding_id = info["binding_id"]
            # 1. tear down any streaming subscription tied to this binding
            self._cleanup_binding_stream(binding_id)
            # 2. best-effort, fire-and-forget notification to the agent's last
            #    known address (only reachable if the agent gave agent_address at
            #    bind/subscribe time — otherwise silently undeliverable).
            try:
                self.swarm.send(info["agent_id"], {
                    "type":            "lease_expired",
                    "binding_id":      binding_id,
                    "capability_name": info["capability_name"],
                    "node_id":         self.node_id,
                    "reason":          "ttl_expired",
                    "expired_at":      time.time(),
                })
            except Exception:
                pass
            print(f"[{self.name}] lease expired binding={binding_id[:8]} "
                  f"cap={info['capability_name']} → slot freed"
                  + (f", auto-granted to {info['next_agent_id'][:8]}" if info.get("next_agent_id") else ""))
        return expired

    def _handle_renew(self, agent_id: str, binding_id: str, capability: str) -> dict:
        """
        Validate ownership + liveness, then extend the lease via the existing
        renew() primitive (broker_renew → verbs.renew). Held under the broker lock
        so the sweeper cannot expire this binding between the check and the renew.
        The new expiry is computed from the DEVICE clock only.
        """
        denied = lambda reason: self._sign({
            "type": "lease_renewed", "status": "denied",
            "binding_id": binding_id, "reason": reason,
        })
        with self.broker._lock:
            b = self.broker.get_binding(binding_id)
            if b is None:
                return denied("unknown_binding")
            if b.agent_id != agent_id:
                return denied("not_owner")
            if capability and b.capability_name != capability:
                return denied("capability_mismatch")
            # A lease that already lapsed (or lost its slot) cannot be renewed —
            # the agent must re-bind. Device clock is the sole authority here.
            if b.status != "active" or time.time() > b.token.expires_at:
                return denied("expired")

            self.broker_renew(binding_id, self.lease_ttl)
            b = self.broker.get_binding(binding_id)

        print(f"[{self.name}] lease renewed binding={binding_id[:8]} "
              f"cap={b.capability_name} ttl={self.lease_ttl}s")
        return self._sign({
            "type": "lease_renewed", "status": "renewed", "binding_id": binding_id,
            "lease_ttl": self.lease_ttl, "lease_expires_at": b.token.expires_at,
            "node_id": self.node_id,
            "token_sig": b.token.signature,
        })

    def publish_capabilities(self) -> None:
        ip, port = self.swarm.address
        for cap in self.advertise():
            record = {
                "node_id":      self.node_id,
                "name":         cap.name,
                "tags":         list(cap.tags),
                "live_state":   {k: v for k, v in cap.live_state.items()},
                "public_key":   self.public_key,
                "address":      [ip, port],
                "device_class": self.device_class,
                "ts":           time.time(),
            }
            self.swarm.publish(signing.sign_record(record, self.private_key, self.public_key))

    # ── binding scope verification ─────────────────────────────────────────────

    def _verify_binding_scope(self, binding_id: str, capability: str) -> bool:
        """Return True iff binding_id is active, in-scope for capability, and not expired."""
        binding = self.broker.get_binding(binding_id)
        if binding is None:
            return False
        if binding.status != "active":
            return False
        if binding.capability_name != capability:
            return False
        if time.time() > binding.token.expires_at:
            binding.status = "expired"
            return False
        return True

    def _cleanup_binding_stream(self, binding_id: str) -> None:
        """Stop any streaming subscription tied to this binding."""
        sub_id = self._binding_subs.pop(binding_id, None)
        if sub_id is not None:
            self.data.unsubscribe(sub_id)

    # ── message handler ────────────────────────────────────────────────────────

    def _sign(self, msg: dict) -> dict:
        """Ed25519-sign an outbound trust message with this device's key
        (v + ts stamped inside the signed payload)."""
        return signing.sign_message(msg, self.private_key, self.public_key)

    def _on_message(self, message: dict) -> dict | None:
        mtype = message.get("type")

        # ── bind request (with policy check) ──────────────────────────────────
        if mtype == "bind_request":
            agent_id = message.get("from_node", "unknown")

            # ── Ed25519 trust gate ────────────────────────────────────────────
            # bind is a trust operation: it MUST be signed by an agent whose
            # node_id derives from its signing key and matches any prior pin.
            # Unsigned (e.g. a 1.0 peer) → hard reject as POLICY (ruling #3),
            # with a reason distinct from version_mismatch.
            reason = signing.verify_message(message, agent_id, self.pins)
            if reason is not None:
                print(f"[{self.name}] bind_request from {agent_id[:8]} REJECTED — {reason}")
                return self._sign({"type": "bind_response", "status": "denied",
                                   "reason": reason,
                                   "message": f"trust check failed: {reason}"})

            cap_name = message.get("capability_name", "")
            needs    = message.get("needs", [])
            priority = message.get("priority", 5)

            # agent_address now rides INSIDE the signed payload — a verified,
            # tamper-evident hint the device may use to push lease_expired notices
            # back to the agent. (Closed TODO: was unauthenticated in the HMAC era.)
            agent_address = message.get("agent_address")
            if agent_address and len(agent_address) == 2:
                self.swarm.add_known_peer(agent_id, agent_address[0], int(agent_address[1]))

            # All TCP bind_requests are remote — always enforce policy
            decision = self.policy.check(cap_name, agent_id, is_remote=True)
            if decision == "deny":
                print(f"[{self.name}] bind_request from {agent_id[:8]} for '{cap_name}' "
                      f"→ denied (policy: blocked)")
                return self._sign({"type": "bind_response", "status": "denied",
                                   "message": "resource blocked by device policy"})
            if decision == "needs_approval":
                if not self.policy.approve(cap_name, agent_id):
                    print(f"[{self.name}] bind_request from {agent_id[:8]} for '{cap_name}' "
                          f"→ denied (sensitive: approval required)")
                    return self._sign({"type": "bind_response", "status": "denied",
                                       "message": "owner approval required for sensitive resource"})

            result = self.broker_request(agent_id, cap_name, needs, priority)
            print(f"[{self.name}] bind_request from {agent_id[:8]} for '{cap_name}' "
                  f"→ {result['status']}")
            if result.get("status") in ("granted", "granted_by_preemption"):
                token = result["token"]
                return self._sign({
                    "type":                 "bind_response",
                    "status":               result["status"],
                    "binding_id":           result.get("binding_id"),
                    "capability_name":      token.capability_name,
                    "agent_id":             token.agent_id,
                    "node_id":              token.node_id,
                    "scope":                token.scope,
                    "expires_at":           token.expires_at,     # kept for back-compat
                    "lease_ttl":            self.lease_ttl,
                    "lease_expires_at":     token.expires_at,      # authoritative (device clock)
                    "token_sig":            token.signature,       # device-signed token artifact
                    "device_class":         self.device_class,
                    "verified_by_provider": True,
                })
            return self._sign({"type": "bind_response", "status": result.get("status"),
                               "message": result.get("message", "")})

        # ── lease renewal (wire-level) ────────────────────────────────────────
        if mtype == "renew_binding":
            agent_id = message.get("from_node", "")
            reason = signing.verify_message(message, agent_id, self.pins)
            if reason is not None:
                print(f"[{self.name}] renew_binding from {agent_id[:8]} REJECTED — {reason}")
                return self._sign({"type": "lease_renewed", "status": "denied",
                                   "binding_id": message.get("binding_id", ""),
                                   "reason": reason})
            return self._handle_renew(
                agent_id=agent_id,
                binding_id=message.get("binding_id", ""),
                capability=message.get("capability_name", ""),
            )

        # ── capability probe (TCP fallback for AP-isolation / probe_peer) ──────
        if mtype == "capabilities_request":
            ip, port = self.swarm.address
            records = []
            for cap in self.advertise():
                records.append(signing.sign_record({
                    "node_id":      self.node_id,
                    "name":         cap.name,
                    "tags":         list(cap.tags),
                    "live_state":   {k: v for k, v in cap.live_state.items()},
                    "public_key":   self.public_key,
                    "address":      [ip, port],
                    "device_class": self.device_class,
                    "ts":           time.time(),
                }, self.private_key, self.public_key))
            return {"type": "capabilities_response", "records": records}

        # ── on-demand data pull (THE DEFAULT) ─────────────────────────────────
        if mtype == "get_reading":
            binding_id = message.get("binding_id", "")
            capability = message.get("capability", "")
            if not self._verify_binding_scope(binding_id, capability):
                return {
                    "type":       "error",
                    "binding_id": binding_id,
                    "error":      "binding_invalid_or_out_of_scope",
                }
            frame = self.data.get_reading(capability)
            return {
                "type":       "reading",
                "capability": capability,
                "binding_id": binding_id,
                "frame":      frame,
            }

        # ── opt-in streaming: subscribe ───────────────────────────────────────
        if mtype == "subscribe":
            binding_id    = message.get("binding_id", "")
            capability    = message.get("capability", "")
            hz            = float(message.get("hz", 5.0))
            agent_node_id = message.get("from_node", "")
            agent_address = message.get("agent_address")

            if not self._verify_binding_scope(binding_id, capability):
                return {
                    "type":       "error",
                    "binding_id": binding_id,
                    "error":      "binding_invalid_or_out_of_scope",
                }

            if agent_address and len(agent_address) == 2:
                self.swarm.add_known_peer(agent_node_id, agent_address[0], int(agent_address[1]))

            def _make_cb(nid: str, cap: str, bid: str):
                def _cb(frame: dict) -> None:
                    self.swarm.send(nid, {
                        "type":       "stream_frame",
                        "capability": cap,
                        "binding_id": bid,
                        "frame":      frame,
                    })
                return _cb

            sub_id = self.data.subscribe(capability, _make_cb(agent_node_id, capability, binding_id), hz)
            self._binding_subs[binding_id] = sub_id
            print(f"[{self.name}] subscribe binding={binding_id[:8]} cap={capability} "
                  f"hz={hz} sub={sub_id[:8]}")
            return {
                "type":       "subscribed",
                "status":     "subscribed",
                "sub_id":     sub_id,
                "binding_id": binding_id,
            }

        # ── opt-in streaming: unsubscribe ─────────────────────────────────────
        if mtype == "unsubscribe":
            binding_id = message.get("binding_id", "")
            self._cleanup_binding_stream(binding_id)
            print(f"[{self.name}] unsubscribe binding={binding_id[:8]}")
            return {"type": "unsubscribed", "status": "ok", "binding_id": binding_id}

        # ── remote release (from ResourceHandle.release) ──────────────────────
        if mtype == "release_binding":
            agent_id = message.get("from_node", "")
            reason = signing.verify_message(message, agent_id, self.pins)
            if reason is not None:
                print(f"[{self.name}] release_binding from {agent_id[:8]} REJECTED — {reason}")
                return self._sign({"type": "released", "status": "denied", "reason": reason})
            cap_name = message.get("capability_name", "")
            result   = self.broker_release(agent_id, cap_name)
            return self._sign({"type": "released", "status": result.get("status", "ok")})

        return None

    # ── broker interface ───────────────────────────────────────────────────────

    def verify_agent_token(self, token: BindToken) -> bool:
        # A device-issued token is verified against the device's OWN public key.
        return verify_bind_token(token, self.public_key)

    def broker_request(self, agent_id: str, capability_name: str,
                       needs: list[str], priority: int = 5) -> dict:
        # Detect preemption before calling broker so we can clean up preempted streams.
        active = self.broker.active_binds.get(capability_name, [])
        quota  = self.broker.quotas.get(capability_name, 1)
        if len(active) >= quota:
            worst = max(active, key=lambda b: b.priority)
            if worst.priority > priority:
                self._cleanup_binding_stream(worst.binding_id)
        return self.broker.request_bind(agent_id, capability_name, needs, priority)

    def broker_release(self, agent_id: str, capability_name: str) -> dict:
        # Clean up streaming before releasing binding.
        active = self.broker.active_binds.get(capability_name, [])
        bind   = next((b for b in active if b.agent_id == agent_id), None)
        if bind:
            self._cleanup_binding_stream(bind.binding_id)
        return self.broker.release_bind(agent_id, capability_name)

    def broker_status(self) -> dict:
        return self.broker.status()

    def broker_rebind(self, binding_id: str, new_capability_name: str) -> dict:
        binding = self.broker.get_binding(binding_id)
        if binding is None:
            return {"status": "error", "message": f"Binding {binding_id} not found"}
        rebind(binding, new_capability_name, self, self.private_key)
        return {
            "status":           "rebound",
            "binding_id":       binding_id,
            "capability_name":  binding.capability_name,
            "rebind_count":     binding.rebind_count,
            "token":            binding.token,
        }

    def broker_renew(self, binding_id: str, ttl_seconds: int = 300) -> dict:
        binding = self.broker.get_binding(binding_id)
        if binding is None:
            return {"status": "error", "message": f"Binding {binding_id} not found"}
        renew(binding, self, self.private_key, ttl_seconds)
        return {"status": "renewed", "binding_id": binding_id, "expires_at": binding.token.expires_at}

    def broker_get_binding(self, binding_id: str) -> dict:
        binding = self.broker.get_binding(binding_id)
        if binding is None:
            return {"status": "error", "message": f"Binding {binding_id} not found"}
        return {
            "binding_id":      binding.binding_id,
            "agent_id":        binding.agent_id,
            "node_id":         binding.node_id,
            "capability_name": binding.capability_name,
            "scope":           binding.scope,
            "created_at":      binding.created_at,
            "rebind_count":    binding.rebind_count,
            "status":          binding.status,
            "expires_at":      binding.token.expires_at,
        }

    # ── sense layer interface ──────────────────────────────────────────────────

    # ── peripheral advertisement (Case 2 generalisation) ─────────────────────

    def attach_peripheral(
        self,
        path: str,
        kind_override: str | None = None,
    ) -> dict:
        """
        Register a peripheral the user/config has explicitly pointed at.

        SECURITY: Only paths passed in here are exposed — the runtime NEVER
        auto-scans /dev or any directory.  Kind is auto-detected via
        detect_kind() unless kind_override is given (for simulation/testing).

        Sensitivity is tied directly into ResourcePolicy consistent with the
        existing camera/mic model:
          open kinds   → policy.allow()          (any remote agent may bind)
          sensitive     → policy.require_approval() (owner approval required;
                          default callback = DENY, exactly like camera/mic)

        Returns the capability-record dict (same shape as
        DumbRelay.capabilities()[0]) or {"error": ...} if unavailable.
        """
        from d2a.guardian.device_kinds import (
            detect_kind, KIND_SENSITIVITY, KIND_PRIMITIVES,
            is_system_input, KIND_UNAVAILABLE, KIND_INPUT_EVENT,
        )

        realpath = os.path.realpath(path)
        kind     = kind_override if kind_override else detect_kind(realpath)

        if kind == KIND_UNAVAILABLE:
            return {"error": "device_unavailable", "path": path}

        sensitivity = KIND_SENSITIVITY.get(kind, "open")
        access      = "consent_required" if sensitivity == "sensitive" else "open"
        sys_input   = is_system_input(realpath) if kind == KIND_INPUT_EVENT else False
        primitives  = KIND_PRIMITIVES.get(kind, [])
        cap_name    = f"raw_{kind}"

        # Tie into ResourcePolicy — same gate as camera/mic
        if access == "consent_required":
            self.policy.require_approval(cap_name)
        else:
            self.policy.allow(cap_name)

        cap = Capability(
            name=cap_name,
            tags=[cap_name, access, "peripheral"],
            live_state={
                "kind":        kind,
                "path":        realpath,
                "primitives":  primitives,
                "access":      access,
                "system_input": sys_input,
            },
            node_id=self.node_id,
            public_key=self.public_key,
        )

        self.capabilities[cap_name]  = cap
        self.broker.quotas[cap_name] = 1
        self._peripheral_paths[realpath] = cap_name

        print(f"[{self.name}] attach_peripheral  path={path!r}  "
              f"kind={kind}  access={access}")

        return {
            "name":        cap_name,
            "kind":        kind,
            "path":        realpath,
            "primitives":  primitives,
            "access":      access,
            "system_input": sys_input,
            "relay_node_id": self.node_id,
        }

    def detach_peripheral(self, path: str) -> dict:
        """
        Remove a previously registered peripheral from the advertised capabilities.
        Live update: immediately drops it from broker quotas and the capability set.
        """
        realpath = os.path.realpath(path)
        cap_name = self._peripheral_paths.pop(realpath, None)
        if cap_name is None:
            return {"error": "peripheral_not_found", "path": path}
        self.capabilities.pop(cap_name, None)
        self.broker.quotas.pop(cap_name, None)
        print(f"[{self.name}] detach_peripheral  path={path!r}  cap={cap_name}")
        return {"status": "detached", "cap_name": cap_name, "path": path}

    # ── relay interface (Case 2 — Capability Guardian) ────────────────────────

    def start_relay(self, resource_name: str | None = None):
        """
        Create a DumbRelay for a raw peripheral detected by resource_probes.

        The relay exposes the device's raw path over D2A with zero intelligence.
        The matching GuardianAgent runs elsewhere — same machine in tests, a
        different node in a real deployment.  Only the transport differs.

        Returns a DumbRelay instance, or None if no suitable peripheral found.
        """
        from d2a.guardian.relay import DumbRelay

        rs = self.resource_snapshot

        # Default: first available storage mount
        if resource_name is None:
            resource_name = next(
                (n for n in ("storage",) if n in rs and rs[n].get("mounts")),
                None,
            )

        path: str | None = None
        if resource_name == "storage" and "storage" in rs:
            mounts = rs["storage"].get("mounts", [])
            path   = mounts[0]["path"] if mounts else "/"
        elif resource_name and resource_name in rs:
            res = rs[resource_name]
            path = res.get("path") or (res.get("nodes") or [None])[0]

        if not path:
            return None

        relay = DumbRelay(node_id=self.node_id, device_path_or_probe=path)
        print(f"[{self.name}] relay started  resource='{resource_name}'  path={path}")
        return relay

    # ── sense layer interface ──────────────────────────────────────────────────

    def sense_reading(
        self,
        resource: str,
        shape: str = "normalized",
        mode: str  = "on_demand",
    ) -> SenseFrame:
        """
        Run the full sense pipeline for a resource and return a SenseFrame.
        Local call — no binding or policy check required.
        Network wiring for remote sense_request messages comes in Part 2.
        """
        return self.sense.handle(SenseRequest(resource=resource, shape=shape, mode=mode))
