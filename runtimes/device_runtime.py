import inspect
import os
import threading
import time
import uuid

from d2a import conditions
from d2a import errors
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
from d2a import manifest as _manifest
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

# ── event layer (v1.3) tunables ────────────────────────────────────────────────
# Device clamp applied to BOTH stream sampling hz and event eval_hz — the device
# owns cadence; an agent asking for 1000 Hz gets MAX_SAMPLE_HZ. One knob, one
# vulnerability closed for both paths.
MAX_SAMPLE_HZ = 10.0
EVENT_SUBS_PER_BINDING   = 8    # what a single live lease may purchase
EVENT_SUBS_PER_CAPABILITY = 32  # device-wide ceiling on one shared loop


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

        # ── event layer (v1.3) ────────────────────────────────────────────────
        # Condition-event subscriptions ride the SAME DataProvider sampling loop
        # and die through the SAME teardown path as streams (see
        # _cleanup_binding_stream). binding_id -> {event_sub_id -> data_sub_id};
        # event_sub_id -> {capability, binding_id, data_sub_id} for the caps.
        self._binding_event_subs: dict[str, dict] = {}
        self._event_meta:         dict[str, dict] = {}
        # Bounded background work, purchased by a live lease. Two guards:
        #   per-binding  — what one lease may buy (default 8).
        #   per-capability — device-wide ceiling on a shared loop (default 32),
        #     defense-in-depth since the loop's cost scales with total subs.
        # Distinct rejection reasons so an agent can tell which limit it hit.
        # Instance attrs (not module constants) so deployments/tests can tune.
        self._event_subs_per_binding = EVENT_SUBS_PER_BINDING
        self._event_cap_ceiling      = EVENT_SUBS_PER_CAPABILITY

        # ── async tasks (v1.3 Phase 2) ────────────────────────────────────────
        # Long-running actions run on a worker thread and return {task_id} now;
        # completion arrives later as a kind:"task" event on the SAME channel.
        # Tasks are binding-scoped: lease death cancels them through the SAME
        # unified teardown path as streams/events (see _cleanup_binding_stream).
        # task_id -> {binding_id, capability, action, status, result, error,
        #             cancel(Event), agent_node_id, created_at}. binding_id ->
        # {task_id, ...} for teardown. Guarded by _tasks_lock.
        self._tasks:         dict[str, dict] = {}
        self._binding_tasks: dict[str, set]  = {}
        self._tasks_lock = threading.Lock()

        # Optional device-LOCAL reflex hook (condition → local action, no agent).
        # Wired on demand via wire_reflex_demo(); consumed by the SenseLayer
        # safety_check hook. Records fired reflexes here for inspection/demo.
        self.reflex_events: list = []

        # Virtual capabilities (Guardian VSO / Synthesis emergent) registered via
        # publish_virtual / publish_emergent. name -> {"reading": fn, "action": fn}.
        # get_reading / the action verb consult this BEFORE the DataProvider, so
        # virtual caps bind & serve through the SAME broker/lease/policy path.
        self._virtual: dict[str, dict] = {}

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
        """
        Graceful departure. Before tearing down the transport we, best-effort:
          1. tear down every active binding through the ONE unified path
             (broker.teardown_all → reason "shutdown"), killing each binding's
             streams / event subs / tasks via _cleanup_binding_stream;
          2. push a `device_shutdown` notice (data-path class, like lease_expired)
             to each affected agent so it can distinguish an ANNOUNCED departure
             from a silent vanish;
          3. unpublish our capability records so discover() drops us immediately
             on both transports — no TTL ghost.
        An ungraceful kill (process death, transport killed without this call) is
        unchanged: no notice, no unpublish, peers TTL-age us exactly as before.
        """
        self._graceful_departure()
        self._sweeper_running = False
        self.swarm.stop()

    # stop() is the natural name and the context-manager exit; both route through
    # the same graceful path as stop_swarm().
    def stop(self) -> None:
        self.stop_swarm()

    def __enter__(self) -> "DeviceRuntime":
        return self

    def __exit__(self, *exc) -> None:
        self.stop_swarm()

    def _graceful_departure(self) -> None:
        if getattr(self, "_departed", False):
            return
        self._departed = True

        # 1 + 2 — unified teardown, then a best-effort shutdown notice per agent.
        try:
            infos = self.broker.teardown_all("shutdown")
        except Exception:
            infos = []
        for info in infos:
            bid = info["binding_id"]
            try:
                self._cleanup_binding_stream(bid)
            except Exception:
                pass
            try:
                self.swarm.send(info["agent_id"], {
                    "type":            "device_shutdown",
                    "binding_id":      bid,
                    "capability_name": info["capability_name"],
                    "node_id":         self.node_id,
                    "code":            errors.DEVICE_SHUTDOWN,
                    "ts":              time.time(),
                })
            except Exception:
                pass

        # 3 — retract every record WE authored (real + virtual caps) so discovery
        # drops us now. Snapshot keys under the lock, then unpublish outside it.
        try:
            with self.swarm._lock:
                own = [(nid, name) for (nid, name) in list(self.swarm.records.keys())
                       if nid == self.node_id]
        except Exception:
            own = []
        for nid, name in own:
            try:
                self.swarm.unpublish({"node_id": nid, "name": name})
            except Exception:
                pass

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
                    "code":            errors.LEASE_EXPIRED,
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
        denied = lambda code: self._sign({
            "type": "lease_renewed", "status": "denied",
            "binding_id": binding_id, "code": code,
        })
        with self.broker._lock:
            b = self.broker.get_binding(binding_id)
            if b is None:
                return denied(errors.UNKNOWN_BINDING)
            if b.agent_id != agent_id:
                return denied(errors.NOT_OWNER)
            if capability and b.capability_name != capability:
                return denied(errors.CAPABILITY_MISMATCH)
            # A lease that already lapsed (or lost its slot) cannot be renewed —
            # the agent must re-bind. Device clock is the sole authority here.
            if b.status != "active" or time.time() > b.token.expires_at:
                return denied(errors.LEASE_EXPIRED)

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

    def _capability_record(self, cap, ip, port) -> dict:
        """
        THE single capability-record builder — used by BOTH the UDP/DHT publish
        path and the TCP capabilities_request path, so the two can never drift
        (e.g. a manifest present over one transport but not the other).

        The manifest (if the capability carries one, or a built-in exists) is
        injected HERE, BEFORE signing, so it rides inside the Ed25519-signed
        bytes and is authenticated for free.
        """
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
        man = cap.manifest if getattr(cap, "manifest", None) else _manifest.builtin_manifest(cap)
        if man is not None:
            record["manifest"] = man
        return signing.sign_record(record, self.private_key, self.public_key)

    def publish_capabilities(self) -> None:
        ip, port = self.swarm.address
        for cap in self.advertise():
            self.swarm.publish(self._capability_record(cap, ip, port))

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
        """
        THE unified data-path teardown for a binding. Stops any streaming
        subscription AND every condition-event subscription tied to this binding.
        Every teardown trigger — lease expiry sweep, explicit release, preemption,
        unsubscribe — already funnels here, so widening this one method (rather
        than adding a parallel event-cleanup path) makes ALL event subscriptions
        die with the binding exactly like stream subscriptions.
        """
        sub_id = self._binding_subs.pop(binding_id, None)
        if sub_id is not None:
            self.data.unsubscribe(sub_id)

        evsubs = self._binding_event_subs.pop(binding_id, {})
        for event_sub_id, data_sub_id in evsubs.items():
            self.data.unsubscribe(data_sub_id)
            self._event_meta.pop(event_sub_id, None)

        # Cancel any running tasks for this binding. HONEST distinction:
        #   - a cooperatively-cancellable action (accepts a cancel token) sees the
        #     Event set and returns early — truly cancelled.
        #   - a non-cancellable action (e.g. the VSO monitor for-loop) keeps
        #     running in the background = ORPHANED; we drop its record so its
        #     completion event is SUPPRESSED, but the loop is not interrupted.
        # Either way the task is gone from the agent's view immediately.
        with self._tasks_lock:
            task_ids = self._binding_tasks.pop(binding_id, set())
            for task_id in task_ids:
                t = self._tasks.pop(task_id, None)
                if t is not None:
                    t["cancel"].set()

    def _cleanup_event_sub(self, binding_id: str, event_sub_id: str) -> bool:
        """Tear down ONE event subscription (explicit unsubscribe_event).
        Returns True if it existed."""
        evsubs = self._binding_event_subs.get(binding_id, {})
        data_sub_id = evsubs.pop(event_sub_id, None)
        if data_sub_id is None:
            return False
        self.data.unsubscribe(data_sub_id)
        self._event_meta.pop(event_sub_id, None)
        if not evsubs:
            self._binding_event_subs.pop(binding_id, None)
        return True

    def _manifest_for(self, capability: str) -> dict | None:
        """The manifest a condition validates against: the capability's own
        manifest (virtual caps) or the shipped built-in (compute/sensing/…)."""
        cap = self.capabilities.get(capability)
        if cap is None:
            return None
        if getattr(cap, "manifest", None):
            return cap.manifest
        return _manifest.builtin_manifest(cap)

    # ── async tasks (v1.3 Phase 2) ──────────────────────────────────────────────

    def _is_long_running(self, capability: str, action: str) -> bool:
        """A dispatcher declares an action long-running via its manifest
        (actions.<name>.long_running == True). Everything else stays synchronous."""
        man = self._manifest_for(capability)
        if not man:
            return False
        spec = (man.get("actions", {}) or {}).get(action, {})
        return bool(spec.get("long_running"))

    @staticmethod
    def _call_action_fn(fn, action: str, params: dict, cancel: threading.Event):
        """
        Invoke a virtual action_fn, passing the cancel token ONLY to functions
        that accept it (arity >= 3). This is the honest orphan/cancel split: a
        cooperative action (action, params, cancel) can stop early; a 2-arg
        action (the VSO monitor) never sees cancel and can only be orphaned.
        """
        try:
            if len(inspect.signature(fn).parameters) >= 3:
                return fn(action, params, cancel)
        except (ValueError, TypeError):
            pass
        return fn(action, params)

    def _run_task(self, task_id: str, v: dict, action: str,
                  params: dict, cancel: threading.Event) -> None:
        """Worker: run the action, then deliver a completion event — UNLESS the
        task was torn down mid-flight (lease death), in which case the record is
        already gone and we suppress delivery (orphan/cancel)."""
        status, result, error = "done", None, None
        try:
            result = self._jsonsafe(self._call_action_fn(v["action"], action, params, cancel))
        except Exception as e:
            status, error = "failed", str(e)
        if cancel.is_set():
            status = "cancelled"

        with self._tasks_lock:
            t = self._tasks.get(task_id)
            if t is None:
                return                      # torn down while running → suppress
            t["status"], t["result"], t["error"] = status, result, error
            agent_node_id = t["agent_node_id"]
            binding_id    = t["binding_id"]
            capability    = t["capability"]

        # Completion delivered on the SAME channel as condition events
        # (kind:"task"); fire-and-forget, unsigned data path.
        try:
            self.swarm.send(agent_node_id, {
                "type":       "event",
                "kind":       "task",
                "capability": capability,
                "binding_id": binding_id,
                "task_id":    task_id,
                "status":     status,
                "result":     result,
                "error_detail": error,   # free-text exception string, NOT a registry code
                "ts":         time.time(),
            })
        except Exception:
            pass

    # ── device-local reflex (v1.3 Phase 2, minimal demo) ────────────────────────

    def wire_reflex_demo(self, verdict: str = "distress") -> None:
        """
        Wire ONE device-LOCAL reflex through the SenseLayer safety_check hook:
        when the health verdict crosses INTO `verdict`, run a local action (here,
        record a flag) with NO agent involved. Reuses conditions.EdgeEvaluator so
        the reflex fires on the edge and re-arms — the same semantics as a wire
        condition, but evaluated and actioned entirely on-device.

        Full reflex POLICY (multiple reflexes, agent-authored bindings) is out of
        scope; this is the hook + one demo, as scoped.
        """
        evaluator = conditions.EdgeEvaluator({"field": "verdict", "op": "eq", "value": verdict})

        def _safety_hook(frame):
            view = {"verdict": frame.verdict, "advice": frame.advice,
                    "confidence": frame.confidence}
            if evaluator.update(view):
                # LOCAL action — no network, no agent. Demo: flag + log.
                self.reflex_events.append({
                    "resource": frame.resource, "verdict": frame.verdict,
                    "advice": frame.advice, "ts": frame.ts,
                })
                print(f"[{self.name}] REFLEX fired verdict={frame.verdict} "
                      f"resource={frame.resource} → local flag (no agent)")
            return frame

        self.sense.set_safety_hook(_safety_hook)

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
                                   "code": reason,
                                   "detail": f"trust check failed: {reason}"})

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
                                   "code": errors.POLICY_BLOCKED,
                                   "detail": "resource blocked by device policy"})
            if decision == "needs_approval":
                if not self.policy.approve(cap_name, agent_id):
                    print(f"[{self.name}] bind_request from {agent_id[:8]} for '{cap_name}' "
                          f"→ denied (sensitive: approval required)")
                    return self._sign({"type": "bind_response", "status": "denied",
                                       "code": errors.APPROVAL_REQUIRED,
                                       "detail": "owner approval required for sensitive resource"})

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
            # Non-grant broker outcome (queued, or an errors.* coded failure like
            # capability_not_found): keep the bind_response type, pass any registry
            # code through, and normalize the human text into `detail`.
            denial = {"type": "bind_response", "status": result.get("status"),
                      "detail": result.get("detail") or result.get("message", "")}
            if result.get("code"):
                denial["code"] = result["code"]
            return self._sign(denial)

        # ── lease renewal (wire-level) ────────────────────────────────────────
        if mtype == "renew_binding":
            agent_id = message.get("from_node", "")
            reason = signing.verify_message(message, agent_id, self.pins)
            if reason is not None:
                print(f"[{self.name}] renew_binding from {agent_id[:8]} REJECTED — {reason}")
                return self._sign({"type": "lease_renewed", "status": "denied",
                                   "binding_id": message.get("binding_id", ""),
                                   "code": reason})
            return self._handle_renew(
                agent_id=agent_id,
                binding_id=message.get("binding_id", ""),
                capability=message.get("capability_name", ""),
            )

        # ── capability probe (TCP fallback for AP-isolation / probe_peer) ──────
        if mtype == "capabilities_request":
            ip, port = self.swarm.address
            records = [self._capability_record(cap, ip, port) for cap in self.advertise()]
            return {"type": "capabilities_response", "records": records}

        # ── on-demand data pull (THE DEFAULT) ─────────────────────────────────
        if mtype == "get_reading":
            binding_id = message.get("binding_id", "")
            capability = message.get("capability", "")
            if not self._verify_binding_scope(binding_id, capability):
                return errors.error(errors.BINDING_INVALID_OR_OUT_OF_SCOPE,
                                    binding_id=binding_id)
            # Virtual capabilities (VSO / emergent) serve their reading through
            # their own dispatcher; real capabilities go to the DataProvider.
            v = self._virtual.get(capability)
            frame = v["reading"]() if v else self.data.get_reading(capability)
            return {
                "type":       "reading",
                "capability": capability,
                "binding_id": binding_id,
                "frame":      frame,
            }

        # ── virtual-capability action invocation (VSO / emergent) ─────────────
        if mtype == "action":
            binding_id = message.get("binding_id", "")
            capability = message.get("capability", "")
            action     = message.get("action", "")
            params     = message.get("params", {}) or {}
            if not self._verify_binding_scope(binding_id, capability):
                return errors.error(errors.BINDING_INVALID_OR_OUT_OF_SCOPE,
                                    binding_id=binding_id)
            v = self._virtual.get(capability)
            if v is None:
                return errors.error(errors.NOT_AN_ACTION_CAPABILITY,
                                    binding_id=binding_id)

            # Long-running action (manifest-declared) → async: return a task_id
            # immediately, deliver completion later as a kind:"task" event. This
            # is what stops a slow monitor from blocking the TCP handler past the
            # agent's 5 s send_and_recv timeout.
            if self._is_long_running(capability, action):
                task_id       = uuid.uuid4().hex
                agent_node_id = message.get("from_node", "")
                cancel        = threading.Event()
                with self._tasks_lock:
                    self._tasks[task_id] = {
                        "binding_id": binding_id, "capability": capability,
                        "action": action, "status": "running", "result": None,
                        "error": None, "cancel": cancel,
                        "agent_node_id": agent_node_id, "created_at": time.time(),
                    }
                    self._binding_tasks.setdefault(binding_id, set()).add(task_id)
                threading.Thread(
                    target=self._run_task,
                    args=(task_id, v, action, params, cancel),
                    daemon=True, name=f"task-{task_id[:8]}",
                ).start()
                print(f"[{self.name}] action(long_running) binding={binding_id[:8]} "
                      f"cap={capability} action={action} → task={task_id[:8]}")
                return {"type": "action_result", "capability": capability,
                        "binding_id": binding_id, "action": action,
                        "result": {"task_id": task_id, "status": "running"}}

            result = v["action"](action, params)
            return {"type": "action_result", "capability": capability,
                    "binding_id": binding_id, "action": action, "result": result}

        # ── async task polling (v1.3 Phase 2) ─────────────────────────────────
        if mtype == "task_status":
            binding_id = message.get("binding_id", "")
            task_id    = message.get("task_id", "")
            with self._tasks_lock:
                t = self._tasks.get(task_id)
                # A task is only visible to its own (still-valid) binding. Once the
                # lease dies the record is dropped → "unknown" (cancelled/gone).
                if t is None or t["binding_id"] != binding_id:
                    return {"type": "task_status", "task_id": task_id,
                            "binding_id": binding_id, "status": "unknown"}
                return {"type": "task_status", "task_id": task_id,
                        "binding_id": binding_id, "status": t["status"],
                        "result": t["result"], "error_detail": t["error"]}

        # ── opt-in streaming: subscribe ───────────────────────────────────────
        if mtype == "subscribe":
            binding_id    = message.get("binding_id", "")
            capability    = message.get("capability", "")
            # Device owns cadence: clamp agent-requested hz to MAX_SAMPLE_HZ.
            # (Was unclamped before v1.3 — a 1000 Hz request would have spun the
            # sampling loop flat out. Same clamp now guards events; see below.)
            hz            = max(0.1, min(float(message.get("hz", 5.0)), MAX_SAMPLE_HZ))
            agent_node_id = message.get("from_node", "")
            agent_address = message.get("agent_address")

            if not self._verify_binding_scope(binding_id, capability):
                return errors.error(errors.BINDING_INVALID_OR_OUT_OF_SCOPE,
                                    binding_id=binding_id)

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
                "type":         "subscribed",
                "status":       "subscribed",
                "sub_id":       sub_id,
                "binding_id":   binding_id,
                "effective_hz": hz,       # echo the clamped rate
            }

        # ── conditional events: subscribe_event (v1.3) ────────────────────────
        if mtype == "subscribe_event":
            binding_id    = message.get("binding_id", "")
            capability    = message.get("capability", "")
            condition     = message.get("condition") or {}
            agent_node_id = message.get("from_node", "")
            agent_address = message.get("agent_address")
            req_hz        = float(message.get("eval_hz", 5.0))

            if not self._verify_binding_scope(binding_id, capability):
                return errors.error(errors.BINDING_INVALID_OR_OUT_OF_SCOPE,
                                    binding_id=binding_id)

            manifest = self._manifest_for(capability)
            if manifest is None:
                return errors.error(errors.NO_MANIFEST_FOR_CONDITIONS,
                                    binding_id=binding_id)
            try:
                cond = conditions.validate_condition(condition, manifest)
            except conditions.ConditionError as e:
                return errors.error(errors.INVALID_CONDITION, str(e),
                                    binding_id=binding_id)

            # Two guards, distinct codes (per-binding is what the lease bought;
            # per-capability is the device ceiling on the shared loop).
            per_binding = self._binding_event_subs.get(binding_id, {})
            if len(per_binding) >= self._event_subs_per_binding:
                return errors.error(errors.EVENT_CAP_EXCEEDED,
                                    f"per-binding limit {self._event_subs_per_binding} reached",
                                    binding_id=binding_id)
            cap_count = sum(1 for m in self._event_meta.values()
                            if m["capability"] == capability)
            if cap_count >= self._event_cap_ceiling:
                return errors.error(errors.DEVICE_EVENT_CAPACITY,
                                    f"per-capability limit {self._event_cap_ceiling} reached",
                                    binding_id=binding_id)

            eval_hz = max(0.1, min(req_hz, MAX_SAMPLE_HZ))
            if agent_address and len(agent_address) == 2:
                self.swarm.add_known_peer(agent_node_id, agent_address[0], int(agent_address[1]))

            event_sub_id = uuid.uuid4().hex
            evaluator    = conditions.EdgeEvaluator(cond)
            seq_box      = {"seq": 0}

            def _event_cb(frame, _ev=evaluator, _esid=event_sub_id, _bid=binding_id,
                          _cap=capability, _nid=agent_node_id, _cond=cond, _box=seq_box):
                # Runs inside the sampling loop. Edge-triggered: emits only on a
                # crossing. Data-path message: binding_id-bearer, NOT signed
                # (same class as stream_frame), fire-and-forget with a per-sub
                # monotonic seq so the agent can detect gaps.
                try:
                    if _ev.update(frame):
                        _box["seq"] += 1
                        self.swarm.send(_nid, {
                            "type":         "event",
                            "capability":   _cap,
                            "binding_id":   _bid,
                            "event_sub_id": _esid,
                            "seq":          _box["seq"],
                            "kind":         "condition",
                            "condition":    _cond,
                            "reading":      frame,       # triggering snapshot
                            "ts":           time.time(),
                        })
                except Exception:
                    pass

            data_sub_id = self.data.subscribe(capability, _event_cb, eval_hz)
            self._binding_event_subs.setdefault(binding_id, {})[event_sub_id] = data_sub_id
            self._event_meta[event_sub_id] = {
                "capability": capability, "binding_id": binding_id, "data_sub_id": data_sub_id,
            }
            print(f"[{self.name}] subscribe_event binding={binding_id[:8]} cap={capability} "
                  f"cond={cond['field']}/{cond['op']} eval_hz={eval_hz} ev={event_sub_id[:8]}")
            return {
                "type":              "event_subscribed",
                "status":            "subscribed",
                "event_sub_id":      event_sub_id,
                "binding_id":        binding_id,
                "effective_eval_hz": eval_hz,   # echo the clamped rate
                "condition":         cond,
            }

        # ── conditional events: unsubscribe_event (v1.3) ──────────────────────
        if mtype == "unsubscribe_event":
            binding_id   = message.get("binding_id", "")
            event_sub_id = message.get("event_sub_id", "")
            existed = self._cleanup_event_sub(binding_id, event_sub_id)
            print(f"[{self.name}] unsubscribe_event binding={binding_id[:8]} "
                  f"ev={event_sub_id[:8]} existed={existed}")
            return {"type": "unsubscribed_event", "status": "ok" if existed else "unknown",
                    "binding_id": binding_id, "event_sub_id": event_sub_id}

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
                return self._sign({"type": "released", "status": "denied", "code": reason})
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
            return {"status": "error", "code": errors.BINDING_NOT_FOUND,
                    "detail": f"Binding {binding_id} not found"}
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
            return {"status": "error", "code": errors.BINDING_NOT_FOUND,
                    "detail": f"Binding {binding_id} not found"}
        renew(binding, self, self.private_key, ttl_seconds)
        return {"status": "renewed", "binding_id": binding_id, "expires_at": binding.token.expires_at}

    def broker_get_binding(self, binding_id: str) -> dict:
        binding = self.broker.get_binding(binding_id)
        if binding is None:
            return {"status": "error", "code": errors.BINDING_NOT_FOUND,
                    "detail": f"Binding {binding_id} not found"}
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

    # ── virtual capabilities on-wire (Case 2 VSO + Case 3 emergent) ───────────

    @staticmethod
    def _jsonsafe(obj):
        """Recursively hex-encode bytes so a virtual action result is JSON/wire safe."""
        if isinstance(obj, bytes):
            return obj.hex()
        if isinstance(obj, dict):
            return {k: DeviceRuntime._jsonsafe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [DeviceRuntime._jsonsafe(v) for v in obj]
        return obj

    def _register_virtual(self, name, kind, consent_tier, manifest, tags,
                          live_state, reading_fn, action_fn) -> dict:
        """
        Register a virtual capability so it binds/serves through the SAME broker,
        lease, and consent path as a real one. Consent tier drives the policy rule
        (sensitive → require_approval, open → allow) — no bypass through the
        virtual path. Publishes the signed record (manifest inside) immediately.
        """
        access = "consent_required" if consent_tier == "sensitive" else "open"
        if consent_tier == "sensitive":
            self.policy.require_approval(name)
        else:
            self.policy.allow(name)

        cap = Capability(
            name=name,
            tags=list(tags),
            live_state=dict(live_state),
            node_id=self.node_id,
            public_key=self.public_key,
            manifest=manifest,
        )
        self.capabilities[name] = cap
        self.broker.quotas[name] = 1
        self._virtual[name] = {"reading": reading_fn, "action": action_fn, "kind": kind}

        # Register the virtual reading fn as a DataProvider pseudo-source so
        # condition-events on this cap ride the SAME shared sampling loop (and the
        # SAME lease teardown) as real capabilities — no parallel evaluator. The
        # reading_fn's keys already match the manifest reading fields, so a
        # condition validates and extracts cleanly against the sampled frame.
        self.data.register_reading_source(name, reading_fn)

        ip, port = self.swarm.address
        self.swarm.publish(self._capability_record(cap, ip, port))
        print(f"[{self.name}] publish_virtual name={name} kind={kind} access={access}")
        return {"name": name, "kind": kind, "access": access, "node_id": self.node_id}

    def publish_virtual(self, vso, name: str | None = None) -> dict:
        """
        Publish a Guardian VirtualSmartObject's SMART surface as a signed,
        discoverable, bindable capability on THIS host node — distinct from the
        raw_<kind> relay capability. The manifest describes the smart actions
        (verdict/monitor/search/…), not the raw primitives.
        """
        kind = vso._kind
        adv  = vso.advertised_capability()
        name = name or adv.get("name", f"smart_{kind}")
        man  = _manifest.smart_manifest(kind)

        def reading_fn():
            return vso.advertised_capability().get("live_state", {})

        def action_fn(action, params):
            return self._jsonsafe(vso.handle_request({"action": action, **(params or {})}))

        return self._register_virtual(
            name, kind, man["consent_tier"], man,
            tags=list(adv.get("tags", [])) + ["virtual_smart_object"],
            live_state=adv.get("live_state", {}),
            reading_fn=reading_fn, action_fn=action_fn,
        )

    def publish_emergent(self, handle, name: str | None = None) -> dict:
        """
        Publish a Synthesis EmergentDeviceHandle as a signed, discoverable,
        bindable capability on THIS (coordinator) node. The record's node_id/
        address are the coordinator's; it is signed with the coordinator's key.
        The manifest is composed from the synthesis kind + combined_contract ONLY
        — member records are NEVER embedded (no per-part leak).
        """
        device = handle._device
        kind   = device.kind
        name   = name or device.name
        man    = _manifest.emergent_manifest(kind, device.combined_contract)

        # live_state carries ONLY the emergent contract — no member node_ids/records.
        live_state = {k: v for k, v in device.live_state.items()
                      if not isinstance(v, (list, dict))}

        def reading_fn():
            return dict(live_state)

        def action_fn(action, params):
            return self._jsonsafe(self._emergent_action(handle, action, params or {}))

        return self._register_virtual(
            name, kind, man["consent_tier"], man,
            tags=[kind, "emergent_device"],
            live_state=live_state,
            reading_fn=reading_fn, action_fn=action_fn,
        )

    def unpublish_derived(self, cap_name: str, code: str) -> list[dict]:
        """
        Retract a PUBLISHED derived capability that can no longer serve (its
        underlying DerivedCapability entered `failed`). Tears down every consumer
        binding through the unified path with a DISTINCT death code (so a consumer
        branches on it apart from a plain lease lapse / device shutdown), stops
        serving the capability, and unpublishes the record so discovery drops it
        immediately. Idempotent. NOTE: reuses the `lease_expired` push *shape*
        (silent-vanish class) but carries `code=derived_input_failed` — the code,
        not the message type, is what the consumer branches on.
        """
        if cap_name not in self._virtual:
            return []
        infos = self.broker.teardown_capability(cap_name, "derived_failed")
        for info in infos:
            bid = info["binding_id"]
            try:
                self._cleanup_binding_stream(bid)
            except Exception:
                pass
            try:
                self.swarm.send(info["agent_id"], {
                    "type":            "lease_expired",
                    "binding_id":      bid,
                    "capability_name": cap_name,
                    "node_id":         self.node_id,
                    "code":            code,
                    "expired_at":      time.time(),
                })
            except Exception:
                pass
        # stop serving: drop the virtual dispatch + capability + quota, then retract
        # the record (both transports) so no new bind/read/discovery can land.
        self._virtual.pop(cap_name, None)
        self.capabilities.pop(cap_name, None)
        self.broker.quotas.pop(cap_name, None)
        try:
            self.swarm.unpublish({"node_id": self.node_id, "name": cap_name})
        except Exception:
            pass
        print(f"[{self.name}] unpublish_derived name={cap_name} code={code} "
              f"→ {len(infos)} consumer binding(s) torn down")
        return infos

    @staticmethod
    def _emergent_action(handle, action, params) -> dict:
        """Dispatch a wire action to the EmergentDeviceHandle. Bytes params are hex."""
        if action == "write":
            return handle.write(params.get("key", ""), bytes.fromhex(params.get("data", "")))
        if action == "read":
            return handle.read(params.get("key", ""))
        if action == "put":
            return handle.put(params.get("key", ""), bytes.fromhex(params.get("value", "")))
        if action == "get":
            return handle.get(params.get("key", ""))
        if action == "read_merged":
            return handle.read_merged(int(params.get("max_per_member", 64)))
        if action == "tail_all":
            return handle.tail_all(int(params.get("lines", 10)))
        if action == "read_all":
            return handle.read_all()
        if action == "verdict_all":
            return handle.verdict_all(float(params.get("warn", 75.0)), float(params.get("danger", 90.0)))
        return {"error": f"unknown_action:{action}"}

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
