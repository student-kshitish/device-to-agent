import hashlib
import inspect
import json
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
from d2a.protocol import PROTOCOL_VERSION
from d2a.resource_probes import probe_resources, RESOURCE_SENSITIVITY
from d2a.policy import ResourcePolicy
from d2a.stream_source import (
    CPUSource, MemorySource, GPUSource,
    ThermalSource, BatterySource, DiskIOSource, NetIOSource,
    CameraMetaSource, MicrophoneMetaSource, LocationMetaSource,
    DisplayMetaSource, StorageSource, NetworkMetaSource,
    DIAGNOSTIC_SOURCES,
)
from d2a.interventions import INTERVENTION_EXECUTORS
from d2a.audit import AuditLog, AuditError
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

# ── node capability catalog (v1.8) ──────────────────────────────────────────────
# Size discipline for the DHT node descriptor (node:<id> names-record). It rides a
# single UDP datagram with exactly ONE provider (the node itself), so the N×record
# ceiling that bounds cap:<name> does NOT apply — but we still cap it to stay well
# clear of the datagram limit and match the manifest-cap philosophy. describe_node
# (TCP, no datagram ceiling) is the complete-catalog fallback when this truncates.
MAX_DESCRIPTOR_NAMES = 256      # open-tier names in one node:<id> record
MAX_DESCRIPTOR_BYTES = 8192     # serialized ceiling; trim names past it


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
        # Owner public key (Phase 10A). Persisted, TOFU-pinned owner identity — a
        # principal DISTINCT from this device's host key. Registered via
        # set_owner_pubkey(); when present it (a) rides the node descriptor header
        # ("this device answers to owner <fp>") and (b) enables REMOTE KEYED
        # approval of intervention plans (an owner signature over the plan_hash, an
        # alternative to the local console callback). None → Phase 8 behaviour
        # unchanged (local callback / default deny).
        self._owner_pin_path = crypto.d2a_home() / f"owner-{name}.json"
        self.owner_pubkey: str | None = self._load_owner_pubkey()
        # Bounded replay guard for accepted owner approvals: nonce -> ts, pruned
        # past the replay window. Stops a captured owner signature being reused.
        self._owner_approval_seen: dict[str, float] = {}
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

        # ── intervention layer (Phase 8) ──────────────────────────────────────
        # MUTATING capabilities registered via attach_intervention. They are
        # DELIBERATELY NOT in self._virtual — the ungated `action` verb must never
        # execute a mutation. Every mutation rides ONLY the propose_intervention
        # verb, through the DOUBLE GATE (bind approval + per-plan owner approval)
        # and the signed audit. name -> {executor, family, target, paired_family}.
        self._interventions: dict[str, dict] = {}
        # Per-plan owner approval callback: fn(plan: dict, agent_id: str) -> bool.
        # Default None → DENY (safe). Distinct from policy.approval_callback so the
        # existing two-tier consent machinery is untouched.
        self._intervention_approval_callback = None
        # Signed append-only audit log (lazily created on first use, so a device
        # that never intervenes writes no file). Keyed by device name + host key.
        self._audit: AuditLog | None = None

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
        # Retract the node descriptor too (v1.8) so node:<id> stops answering
        # "what does node X offer" the moment we leave — no TTL ghost.
        try:
            self.swarm.unpublish_node_descriptor(self.node_id)
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

        # Sweep delegated children whose OWN (capped) lease lapsed before their
        # parent's teardown — so a child never over-serves past its cap even if the
        # parent is still alive. Same unified cleanup + best-effort notice.
        for info in self.broker.sweep_expired_delegations():
            self._cleanup_binding_stream(info["binding_id"])
            self._notify_delegation_ended(info, errors.LEASE_EXPIRED)
            print(f"[{self.name}] delegation expired binding={info['binding_id'][:8]} "
                  f"cap={info['capability_name']} (child lease lapsed)")
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
            # A delegated CHILD is non-renewable (Phase 10B): its right cannot be
            # extended past — or independently of — the parent's lease. The
            # delegate must ask the delegator to re-delegate. (The parent's owner
            # renewing the parent is what keeps the umbrella alive.)
            if b.parent_binding_id:
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

    def _cap_manifest(self, cap) -> dict | None:
        """
        THE single source of a capability's manifest: its own attached manifest
        (virtual / diagnostic / intervention / derived) or the shipped built-in
        (compute / sensing / raw_*), else None. Anti-drift rule (v1.8): BOTH the
        published capability record (_capability_record) AND the describe_node
        catalog route through this ONE expression, so the on-wire manifest and the
        catalog manifest can never disagree.
        """
        return cap.manifest if getattr(cap, "manifest", None) else _manifest.builtin_manifest(cap)

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
        man = self._cap_manifest(cap)
        if man is not None:
            record["manifest"] = man
        return signing.sign_record(record, self.private_key, self.public_key)

    def publish_capabilities(self) -> None:
        ip, port = self.swarm.address
        for cap in self.advertise():
            self.swarm.publish(self._capability_record(cap, ip, port))
        # v1.8: (re)publish the node descriptor (open-tier names under node:<id>)
        # AFTER the caps so its name list reflects the freshly published set.
        self._publish_node_descriptor()

    # ── node capability catalog (v1.8) ──────────────────────────────────────────

    def _cap_tier(self, cap) -> str:
        """The capability's consent tier ('open'|'sensitive'|'intervention').
        From its manifest's consent_tier (the SSOT-validated value), else the
        resource's intrinsic sensitivity (unknown → 'sensitive', safe)."""
        man = self._cap_manifest(cap)
        if man and man.get("consent_tier"):
            return man["consent_tier"]
        return _manifest.consent_tier_for_resource(cap.name)

    def _catalog_entry(self, cap) -> dict:
        """One catalog row: name + tier + full manifest (via the SAME _cap_manifest
        expression the published record uses — anti-drift) + tags."""
        return {
            "name":     cap.name,
            "tier":     self._cap_tier(cap),
            "tags":     list(cap.tags),
            "manifest": self._cap_manifest(cap),   # may be None (additive contract)
        }

    def _catalog_for(self, requester_id: str) -> list[dict]:
        """
        Assemble the consent-filtered catalog. ONE PREDICATE (v1.8, binding): a
        capability is disclosed IFF it would pass the bind gate's static half right
        now — policy.check(name, requester, is_remote=True) == "allow". Nothing
        else. needs_approval / deny entries are OMITTED ENTIRELY (not name-only),
        so an unauthorized agent cannot even tell they exist. Visibility never
        exceeds bind-ability, and it flips EXACTLY when policy.check flips.

        This SAME predicate builds the describe_node catalog AND the node:<id>
        names-record — no second/parallel visibility rule anywhere, so the two
        surfaces can never disagree. It is a READ: it calls policy.check (the
        static rule table) and NEVER policy.approve (no owner prompt — a catalog
        request that prompts the owner would be a DoS).

        `requester_id` is passed straight to policy.check. Today the policy is
        agent-agnostic (rules are per-resource), so the disclosed set is the same
        for every requester; the id is threaded through so a future per-agent
        policy tightens BOTH the catalog and binding through this one call. A cap
        the owner has opened with policy.allow() (e.g. camera) becomes bindable by
        any remote agent AND therefore enumerable — consistent, by design. A cap
        left at needs_approval (the default for every sensitive / intervention
        capability) stays un-bindable-without-consent AND invisible.
        """
        return [self._catalog_entry(cap) for cap in self.advertise()
                if self.policy.check(cap.name, requester_id, is_remote=True) == "allow"]

    def _node_header(self, catalog_count: int, catalog_truncated: bool) -> dict:
        """The node self-descriptor header (ruling 2): one answer to 'what is this
        node'. owner_pubkey is omitted while unregistered (forward hook)."""
        hdr = {
            "node_id":           self.node_id,
            "protocol_version":  PROTOCOL_VERSION,
            "device_class":      self.device_class,
            "host_pubkey":       self.public_key,
            "catalog_ts":        time.time(),
            "catalog_count":     catalog_count,
            "catalog_truncated": catalog_truncated,
        }
        if self.owner_pubkey:
            hdr["owner_pubkey"] = self.owner_pubkey
        return hdr

    def _node_descriptor(self) -> dict:
        """
        Build the signed node descriptor for the node:<id> names-record: the
        disclosed capability names (the SAME _catalog_for predicate the
        describe_node catalog uses — one predicate, both surfaces) plus the
        address (retained for _resolve_peer). The names-record is world-readable,
        so it carries names only — never manifests — and, like the catalog, omits
        any cap the owner hasn't opened for binding. Size-disciplined: truncated
        to MAX_DESCRIPTOR_NAMES / MAX_DESCRIPTOR_BYTES with truncated:true past it.
        Signed with sign_record (ts excluded, TTL-managed) like every DHT record.
        """
        names = [e["name"] for e in self._catalog_for("")]
        names.sort()
        truncated = False
        if len(names) > MAX_DESCRIPTOR_NAMES:
            names = names[:MAX_DESCRIPTOR_NAMES]
            truncated = True
        try:
            ip, port = self.swarm.address
        except Exception:
            ip, port = ("0.0.0.0", 0)

        def _signed(nm, trunc):
            return signing.sign_record({
                "node_id":          self.node_id,
                "node_descriptor":  True,
                "device_class":     self.device_class,
                "public_key":       self.public_key,
                "address":          [ip, port],
                "capability_names": list(nm),
                "truncated":        trunc,
            }, self.private_key, self.public_key)

        # Enforce the serialized byte ceiling on the SIGNED record (the on-wire
        # shape — the signature/keys are ~200 B of fixed overhead) by trimming
        # names, the cheapest signal to drop; the full catalog is still reachable
        # via describe_node over TCP, which has no datagram ceiling.
        import json as _json
        signed = _signed(names, truncated)
        while len(_json.dumps(signed, default=str).encode()) > MAX_DESCRIPTOR_BYTES and names:
            names.pop()
            truncated = True
            signed = _signed(names, truncated)
        return signed

    def _publish_node_descriptor(self) -> None:
        """(Re)publish the node descriptor whenever the offered capability set
        changes. Best-effort + no-op on transports without a keyed node record
        (LANSwarm carries every open cap record on the wire already)."""
        try:
            self.swarm.publish_node_descriptor(self._node_descriptor())
        except Exception:
            pass

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

    def _scope_allows(self, binding_id: str, action: str) -> bool:
        """Phase 10B scope check for an action/mutation. A normal binding (no
        scope_restrict) allows everything the capability offers — a no-op, so
        existing binds are unaffected. A NARROWED delegation allows only the
        actions in its scope_restrict['actions'] allow-list (never wider than the
        capability). None/absent list → full inheritance."""
        b = self.broker.get_binding(binding_id)
        if b is None or not b.scope_restrict:
            return True
        actions = b.scope_restrict.get("actions")
        if actions is None:
            return True
        return action in actions

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

        # ── delegation cascade (Phase 10B) ────────────────────────────────────
        # A delegated child's right cannot outlive its parent's lease. Whichever
        # path tore THIS binding down (release / expiry / preemption / shutdown /
        # capability-teardown) funnels here, so revoking + cleaning up its children
        # HERE makes the cascade automatic for every path. Re-delegation is
        # forbidden, so the recursion is depth-1, but it is written generally.
        for info in self.broker.revoke_children(binding_id, "revoked"):
            child_id = info["binding_id"]
            self._notify_delegation_ended(info, errors.DELEGATION_REVOKED)
            self._cleanup_binding_stream(child_id)

    def _notify_delegation_ended(self, info: dict, code: str) -> None:
        """Best-effort push telling a delegate B its child right ended (revoke or
        parent-gone cascade). Reuses the lease-death shape so B's existing handler
        marks the binding lost; only reachable if B's address is known."""
        try:
            self.swarm.send(info["agent_id"], {
                "type":            "lease_expired",
                "binding_id":      info["binding_id"],
                "capability_name": info["capability_name"],
                "node_id":         self.node_id,
                "code":            code,
                "expired_at":      time.time(),
            })
        except Exception:
            pass

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

        # ── node capability catalog (v1.8 — the list_tools / agent-card verb) ──
        # Point-to-point: an agent that can REACH this node asks it directly for
        # its FULL, consent-filtered catalog + a node self-descriptor. The whole
        # response is host-key-signed (ONE signature over header + catalog), so the
        # agent verifies + TOFU-pins it exactly like a bind_response.
        #
        # Disclosure is the ONE PREDICATE (policy.check == "allow", via
        # _catalog_for): a cap is listed IFF the requester could bind it right now.
        # A describe is a pure READ — it consults the static rule table and NEVER
        # policy.approve, so it can never prompt the owner (that would be a DoS).
        # The signed `from_node` is threaded to policy.check (agent-agnostic today;
        # a future per-agent policy narrows the catalog through the same call). We
        # deliberately do NOT verify/pin the requester here — describe leaves no
        # trust side effect; the RESPONSE is what carries authenticity.
        if mtype == "describe_node":
            requester = message.get("from_node", "") or ""
            catalog = self._catalog_for(requester)
            header  = self._node_header(catalog_count=len(catalog), catalog_truncated=False)
            print(f"[{self.name}] describe_node from {(requester or 'anon')[:8]} "
                  f"→ {len(catalog)} cap(s)")
            return self._sign({"type": "describe_node_response",
                               "node": header, "catalog": catalog})

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
            # Delegation scope (Phase 10B): a narrowed delegation may invoke only the
            # actions in its allow-list. A normal binding allows everything (no-op).
            if not self._scope_allows(binding_id, action):
                return errors.error(errors.DELEGATION_SCOPE_EXCEEDED,
                                    f"action {action!r} is outside this delegated scope",
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

        # ── intervention: propose_intervention (Phase 8, MUTATING) ────────────
        # TOCTOU-free single round: propose → owner gate → execute → device-run
        # verify → signed audit → result, all here. The agent never re-submits plan
        # fields between approval and execution — the device acts on the exact
        # normalized plan it hashed. EVERY terminal path is audited (deny too).
        if mtype == "propose_intervention":
            binding_id = message.get("binding_id", "")
            capability = message.get("capability", "")
            plan       = message.get("plan")
            agent_id   = message.get("from_node", "")

            # Type-validate the verb entry up front (rider): a malformed request is
            # a clean INVALID_PLAN, never a handler crash.
            if not isinstance(binding_id, str):
                binding_id = ""
            if not isinstance(capability, str) or not capability:
                return errors.error(errors.INVALID_PLAN, "capability must be a string",
                                    binding_id=binding_id)

            if not self._verify_binding_scope(binding_id, capability):
                return errors.error(errors.BINDING_INVALID_OR_OUT_OF_SCOPE,
                                    binding_id=binding_id)

            intv = self._interventions.get(capability)
            if intv is None:
                return errors.error(errors.NOT_AN_INTERVENTION_CAPABILITY,
                                    binding_id=binding_id)

            ok, why, nplan = self._validate_plan(capability, plan)
            if not ok:
                return errors.error(errors.INVALID_PLAN, why, binding_id=binding_id)

            # Delegation scope (Phase 10B): a narrowed intervention delegation may
            # propose only the mutating actions in its allow-list — checked BEFORE
            # the owner gate so a B can't even prompt for an out-of-scope action.
            if not self._scope_allows(binding_id, nplan["action"]):
                return errors.error(errors.DELEGATION_SCOPE_EXCEEDED,
                                    f"action {nplan['action']!r} is outside this delegated scope",
                                    binding_id=binding_id)

            plan_hash = hashlib.sha256(crypto.canonical_json(nplan)).hexdigest()

            def _result(status, *, approved, executed, verify, detail="", code=None,
                        audit_seq=None):
                out = {
                    "type":           "intervention_result",
                    "capability":     capability, "binding_id": binding_id,
                    "plan_hash":      plan_hash, "status": status,
                    "approved":       approved, "executed": executed,
                    "verify":         verify,
                    "reversible":     nplan["reversible"],
                    "reversible_how": nplan["reversible_how"],
                    "detail":         detail, "audit_seq": audit_seq,
                }
                if code:
                    out["code"] = code
                return out

            _NO_VERIFY = {"ran": False, "passed": False,
                          "condition": nplan["verify"]["condition"], "reading": {}}

            # FAIL-CLOSED: never intervene (not even prompt) on top of a tampered
            # audit chain — a mutation we cannot record must not happen.
            chain_ok, chain_detail = self._audit_log().verify_chain()
            if not chain_ok:
                print(f"[{self.name}] propose_intervention REFUSED — audit chain broken: {chain_detail}")
                return errors.error(errors.AUDIT_SEALED, chain_detail, binding_id=binding_id)

            # Pre-flight (privilege / tool availability) — refuse BEFORE mutating.
            pf_ok, pf_reason = intv["executor"].preflight()
            if not pf_ok:
                entry = self._audit_intervention(
                    agent_id=agent_id, capability=capability, plan=nplan,
                    plan_hash=plan_hash, approved=False, executed=False,
                    verify_outcome="not_run", result_status="refused_preflight",
                    detail=pf_reason)
                return _result("refused_preflight", approved=False, executed=False,
                               verify=_NO_VERIFY, detail=pf_reason,
                               code=errors.INTERVENTION_PREFLIGHT_REFUSED,
                               audit_seq=(entry or {}).get("seq"))

            # PER-PLAN owner approval (second gate; default DENY). Local console
            # callback OR a remote KEYED owner signature (Phase 10A) — one gate,
            # resolved here. The owner sees the FULL normalized plan.
            decision = self._resolve_plan_approval(nplan, plan_hash, agent_id, message)

            # PENDING (10A round 1): keyed approval is required but no signature was
            # attached. Hand the owner the exact plan_hash + nonce to sign and
            # resubmit. NON-TERMINAL — nothing mutates, nothing is audited yet.
            if decision["kind"] == "pending":
                print(f"[{self.name}] propose_intervention PENDING owner signature "
                      f"cap={capability} action={nplan['action']}")
                out = _result("pending_owner_approval", approved=False, executed=False,
                              verify=_NO_VERIFY, detail="owner signature required",
                              code=errors.OWNER_APPROVAL_REQUIRED)
                out["owner_approval_request"] = {
                    "plan_hash":      plan_hash,
                    "device_node_id": self.node_id,
                    "owner_pubkey":   self.owner_pubkey,
                    "nonce":          decision["nonce"],
                    "ts":             decision["ts"],
                }
                return out

            if not decision["approved"]:
                # A keyed denial carries a distinct code (bad sig / stale / mismatch);
                # a local-callback denial keeps the Phase 8 approval_required code.
                deny_code   = decision.get("code", errors.APPROVAL_REQUIRED)
                deny_detail = decision.get("detail", "owner declined this plan")
                entry = self._audit_intervention(
                    agent_id=agent_id, capability=capability, plan=nplan,
                    plan_hash=plan_hash, approved=False, executed=False,
                    verify_outcome="not_run", result_status="denied",
                    detail=deny_detail, approval=decision)
                print(f"[{self.name}] propose_intervention DENIED ({decision['kind']}) "
                      f"cap={capability} action={nplan['action']} — {deny_detail}")
                return _result("denied", approved=False, executed=False,
                               verify=_NO_VERIFY, detail=deny_detail,
                               code=deny_code,
                               audit_seq=(entry or {}).get("seq"))

            # APPROVED → execute the mutation, then the DEVICE runs the declared
            # verify itself (never trusted from the agent).
            exec_res = intv["executor"].execute(nplan["action"], nplan["params"])
            executed = bool(exec_res.get("ok"))

            if not executed:
                entry = self._audit_intervention(
                    agent_id=agent_id, capability=capability, plan=nplan,
                    plan_hash=plan_hash, approved=True, executed=False,
                    verify_outcome="not_run", result_status="error",
                    detail=exec_res.get("detail", ""), approval=decision)
                return _result("error", approved=True, executed=False,
                               verify=_NO_VERIFY, detail=exec_res.get("detail", ""),
                               code=errors.INTERVENTION_ERROR,
                               audit_seq=(entry or {}).get("seq"))

            verify = self._run_verify(intv, nplan)
            status = "executed" if verify["passed"] else "failed_verify"
            entry = self._audit_intervention(
                agent_id=agent_id, capability=capability, plan=nplan,
                plan_hash=plan_hash, approved=True, executed=True,
                verify_outcome=("pass" if verify["passed"] else "fail"),
                result_status=status, verify_reading=verify.get("reading"),
                detail=exec_res.get("detail", ""), approval=decision)
            print(f"[{self.name}] propose_intervention {status.upper()} "
                  f"cap={capability} action={nplan['action']} verify={verify['passed']}")
            return _result(status, approved=True, executed=True, verify=verify,
                           detail=exec_res.get("detail", ""),
                           code=(None if verify["passed"] else errors.INTERVENTION_VERIFY_FAILED),
                           audit_seq=(entry or {}).get("seq"))

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

        # ── lease delegation: delegate_binding (Phase 10B, trust op) ───────────
        # Agent A hands a binding to agent B. Signed by A. The device RE-GATES B
        # (consent is never laundered), caps B's child lease to A's remaining lease,
        # optionally narrows scope, and links the child for cascade teardown.
        if mtype == "delegate_binding":
            agent_id = message.get("from_node", "")
            reason = signing.verify_message(message, agent_id, self.pins)
            if reason is not None:
                return self._sign({"type": "delegation_result", "status": "denied", "code": reason})
            return self._sign(self._handle_delegate(agent_id, message))

        # ── lease delegation: revoke_delegation (Phase 10B, trust op) ──────────
        if mtype == "revoke_delegation":
            agent_id = message.get("from_node", "")
            reason = signing.verify_message(message, agent_id, self.pins)
            if reason is not None:
                return self._sign({"type": "delegation_revoked", "status": "denied", "code": reason})
            return self._sign(self._handle_revoke_delegation(agent_id, message))

        return None

    # ── lease delegation handlers (Phase 10B) ───────────────────────────────────

    def _handle_delegate(self, agent_id: str, message: dict) -> dict:
        """Issue a re-gated, lease-capped, optionally scope-narrowed CHILD binding
        for a delegate B under A's parent binding. agent_id is the VERIFIED A."""
        parent_id = message.get("parent_binding_id", "")
        delegate  = message.get("delegate_agent_id", "")
        cap       = message.get("capability", "")
        scope     = message.get("scope")                 # {"actions":[...]} or None
        sub_ttl   = message.get("sub_ttl")               # seconds, optional
        deleg_addr = message.get("delegate_address")

        def denied(code, detail=""):
            return {"type": "delegation_result", "status": "denied",
                    "code": code, "detail": detail, "parent_binding_id": parent_id}

        if not isinstance(delegate, str) or not delegate:
            return denied(errors.INVALID_PLAN, "delegate_agent_id required")

        with self.broker._lock:
            parent = self.broker.get_binding(parent_id)
            # A must OWN a live parent binding for this capability.
            if parent is None or parent.status != "active":
                return denied(errors.BINDING_INVALID_OR_OUT_OF_SCOPE, "parent binding invalid")
            if parent.agent_id != agent_id:
                return denied(errors.NOT_DELEGATOR, "requester does not own the parent binding")
            if cap and parent.capability_name != cap:
                return denied(errors.CAPABILITY_MISMATCH, "capability does not match the parent binding")
            cap = parent.capability_name
            if parent.parent_binding_id:
                # No re-delegation (v1): a child cannot itself be a delegation parent.
                return denied(errors.NOT_DELEGATOR, "a delegated binding cannot be re-delegated")
            now = time.time()
            if now > parent.token.expires_at:
                return denied(errors.LEASE_EXPIRED, "parent lease has expired")

            # Scope: never wider than the capability's declared actions.
            scope_restrict = None
            if scope is not None:
                if not isinstance(scope, dict):
                    return denied(errors.DELEGATION_SCOPE_EXCEEDED, "scope must be an object")
                actions = scope.get("actions")
                if actions is not None:
                    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
                        return denied(errors.DELEGATION_SCOPE_EXCEEDED, "scope.actions must be a list of strings")
                    man = self._cap_manifest(self.capabilities.get(cap)) if self.capabilities.get(cap) else None
                    declared = set((man or {}).get("actions", {}) or {})
                    if declared and not set(actions) <= declared:
                        return denied(errors.DELEGATION_SCOPE_EXCEEDED,
                                      f"actions {sorted(set(actions) - declared)} not offered by {cap}")
                    scope_restrict = {"actions": list(actions)}

            # RE-GATE B by tier — consent is never laundered from A to B.
            tier = self._cap_tier(self.capabilities.get(cap)) if self.capabilities.get(cap) else "sensitive"
            approval = None
            if tier == "intervention":
                approval = self._verify_delegation_approval(
                    message.get("owner_approval"), cap, delegate, parent_id)
                if not approval["approved"]:
                    return denied(approval.get("code", errors.APPROVAL_REQUIRED),
                                  approval.get("detail", "owner approval required to delegate an intervention right"))
            else:
                # open → allow; sensitive → the SAME policy gate a direct bind by B faces.
                decision = self.policy.check(cap, delegate, is_remote=True)
                if decision == "deny":
                    return denied(errors.POLICY_BLOCKED, "resource blocked by device policy for the delegate")
                if decision == "needs_approval" and not self.policy.approve(cap, delegate):
                    return denied(errors.APPROVAL_REQUIRED, "owner approval required for the delegate at this tier")

            # Cap the child lease to the parent's remaining lease (never longer).
            expires_at = parent.token.expires_at
            if isinstance(sub_ttl, (int, float)) and not isinstance(sub_ttl, bool) and sub_ttl > 0:
                expires_at = min(expires_at, now + float(sub_ttl))

            issued = self.broker.issue_delegation(parent_id, delegate, scope_restrict, expires_at)

        # Best-effort: learn B's address so revoke/cascade notices can reach it.
        if isinstance(deleg_addr, (list, tuple)) and len(deleg_addr) == 2:
            self.swarm.add_known_peer(delegate, deleg_addr[0], int(deleg_addr[1]))

        # Intervention-tier delegations are AUDITED (who delegated what to whom).
        if tier == "intervention":
            self._audit_delegation(delegator=agent_id, delegate=delegate, capability=cap,
                                   parent_binding_id=parent_id, child_binding_id=issued["binding_id"],
                                   scope_restrict=scope_restrict, approval=approval)

        token = issued["token"]
        print(f"[{self.name}] delegate_binding {agent_id[:8]} → {delegate[:8]} cap={cap} "
              f"tier={tier} scope={scope_restrict} child={issued['binding_id'][:8]}")
        return {
            "type":              "delegation_result",
            "status":            "delegated",
            "capability":        cap,
            "parent_binding_id": parent_id,
            "binding_id":        issued["binding_id"],
            "delegate_agent_id": delegate,
            "scope":             scope_restrict,
            "lease_expires_at":  issued["expires_at"],
            "token_sig":         token.signature,
            "node_id":           self.node_id,
            "device_class":      self.device_class,
        }

    def _handle_revoke_delegation(self, agent_id: str, message: dict) -> dict:
        """Revoke ONE delegation. Allowed to the DELEGATOR (A) or the owner-of-record
        (the parent's agent). Tears the child down through the unified path."""
        child_id = message.get("binding_id", "")
        b = self.broker.get_binding(child_id)
        if b is None or not b.parent_binding_id:
            return {"type": "delegation_revoked", "status": "unknown",
                    "code": errors.DELEGATION_NOT_FOUND, "binding_id": child_id}
        parent = self.broker.get_binding(b.parent_binding_id)
        delegator = parent.agent_id if parent else b.delegated_by
        if agent_id != delegator:
            return {"type": "delegation_revoked", "status": "denied",
                    "code": errors.NOT_DELEGATOR, "binding_id": child_id}
        info = self.broker.revoke_one_delegation(child_id, "revoked")
        if info is None:
            return {"type": "delegation_revoked", "status": "unknown",
                    "code": errors.DELEGATION_NOT_FOUND, "binding_id": child_id}
        self._cleanup_binding_stream(child_id)
        self._notify_delegation_ended(info, errors.DELEGATION_REVOKED)
        print(f"[{self.name}] revoke_delegation {agent_id[:8]} → child={child_id[:8]} cut off")
        return {"type": "delegation_revoked", "status": "revoked", "binding_id": child_id}

    def _audit_delegation(self, *, delegator: str, delegate: str, capability: str,
                          parent_binding_id: str, child_binding_id: str,
                          scope_restrict: dict | None, approval: dict | None) -> dict | None:
        """Append ONE signed audit entry for an intervention-tier delegation — the
        mutation-authority trail records who delegated a PROPOSE right to whom, with
        the owner pubkey + signature that sanctioned it."""
        entry = {
            "kind":              "delegation",
            "delegator":         delegator,
            "delegate":          delegate,
            "device_node_id":    self.node_id,
            "capability":        capability,
            "parent_binding_id": parent_binding_id,
            "child_binding_id":  child_binding_id,
            "scope_restrict":    scope_restrict,
            "approver":          (approval or {}).get("approver", ""),
            "owner_pubkey":      (approval or {}).get("owner_pubkey"),
            "owner_sig":         (approval or {}).get("owner_sig"),
            "ts":                time.time(),
        }
        try:
            return self._audit_log().append(entry)
        except AuditError:
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

    # ── diagnostics on-wire (Phase 7 — read-only self-inspection) ─────────────

    @staticmethod
    def _diag_slug(s: str) -> str:
        """Filesystem/wire-safe slug for a diagnostic target (used in cap name)."""
        return "".join(c if c.isalnum() else "_" for c in s).strip("_").lower() or "target"

    def attach_diagnostic(self, family: str, target: str,
                          name: str | None = None, **opts) -> dict:
        """
        Publish a READ-ONLY diagnostic as a normal manifested, readable,
        condition-subscribable capability on THIS node.

        A diagnostic lets an agent SEE a subsystem's failure state BEFORE any fix
        is attempted — it is the read-only half of the fix loop (intervention is a
        later phase). `family` is one of DIAGNOSTIC_SOURCES; `target` names the
        concrete subsystem (a device node path, kernel module, systemd unit, or
        USB bus id). `opts` pass through to the source (e.g. user=True for a
        --user service, dmesg_tail=N for a module).

        Same risk class as the existing probes: reads /proc, /sys, or a read-only
        query to a standard tool. It NEVER mutates state. Diagnostics are sensitive
        (system introspection reveals running processes / device inventory), so a
        remote agent is DENIED by default and needs explicit owner approval —
        exactly like camera/microphone.

        Returns the capability descriptor, or {"error": ...} for an unknown family.
        """
        cls = DIAGNOSTIC_SOURCES.get(family)
        if cls is None:
            return {"error": "unknown_diagnostic_family", "family": family,
                    "known": sorted(DIAGNOSTIC_SOURCES)}

        source   = cls(target, **opts)
        man      = _manifest.diagnostic_manifest(family, target)
        tier     = man["consent_tier"]                       # always "sensitive"
        cap_name = name or f"diag_{family}_{self._diag_slug(target)}"
        access   = "consent_required" if tier == "sensitive" else "open"

        # Register the read-only source so get_reading + the shared sampling loop
        # (streams AND condition-events) drive it exactly like a hardware source.
        self.data._sources[cap_name] = [source]

        # Consent gate — same path as attach_peripheral / camera-mic. Sensitive →
        # require_approval (default callback DENIES), so a remote agent can't bind
        # this introspection surface until the owner opts in.
        if tier == "sensitive":
            self.policy.require_approval(cap_name)
        else:
            self.policy.allow(cap_name)

        cap = Capability(
            name=cap_name,
            tags=[cap_name, access, "diagnostic", family],
            live_state={"family": family, "target": target, "access": access,
                        "read_only": True},
            node_id=self.node_id,
            public_key=self.public_key,
            manifest=man,
        )
        self.capabilities[cap_name]  = cap
        self.broker.quotas[cap_name] = 1

        # Best-effort immediate publish (both transports) if the swarm is up; a
        # diagnostic attached before start_swarm is picked up by publish_capabilities.
        try:
            ip, port = self.swarm.address
            self.swarm.publish(self._capability_record(cap, ip, port))
            self._publish_node_descriptor()                # offered set changed
        except Exception:
            pass

        print(f"[{self.name}] attach_diagnostic  family={family}  target={target!r}  "
              f"cap={cap_name}  access={access}")
        return {"name": cap_name, "family": family, "target": target,
                "access": access, "consent_tier": tier, "node_id": self.node_id}

    def detach_diagnostic(self, cap_name: str) -> dict:
        """Remove a previously attached diagnostic: tears down any binding's
        streams/events through the unified path, drops the source + capability +
        quota, and unpublishes the record so discovery drops it immediately."""
        if cap_name not in self.capabilities or "diagnostic" not in self.capabilities[cap_name].tags:
            return {"error": "diagnostic_not_found", "cap_name": cap_name}
        try:
            infos = self.broker.teardown_capability(cap_name, "shutdown")
        except Exception:
            infos = []
        for info in infos:
            try:
                self._cleanup_binding_stream(info["binding_id"])
            except Exception:
                pass
        self.capabilities.pop(cap_name, None)
        self.broker.quotas.pop(cap_name, None)
        self.data._sources.pop(cap_name, None)
        try:
            self.swarm.unpublish({"node_id": self.node_id, "name": cap_name})
            self._publish_node_descriptor()                # offered set changed
        except Exception:
            pass
        print(f"[{self.name}] detach_diagnostic  cap={cap_name}")
        return {"status": "detached", "cap_name": cap_name}

    # ── intervention layer on-wire (Phase 8 — MUTATING, double-gated) ─────────

    def set_intervention_approval_callback(self, fn) -> None:
        """Wire the PER-PLAN owner approval gate: fn(plan: dict, agent_id: str) ->
        bool. Called for every propose_intervention AFTER preflight. Default (no
        callback) DENIES — nothing mutates without an explicit owner yes. The
        callback receives the FULL plan (including reversible / reversible_how) so
        the owner approves a SPECIFIC plan, not a resource name.

        Coexists with remote KEYED approval (Phase 10A): if a request carries an
        owner signature it takes precedence; this local console callback is the
        fallback used when no signature is attached."""
        self._intervention_approval_callback = fn

    # ── owner identity (Phase 10A — remote keyed approval) ────────────────────

    def _load_owner_pubkey(self) -> str | None:
        try:
            if self._owner_pin_path.exists():
                return json.loads(self._owner_pin_path.read_text()).get("owner_pubkey")
        except (OSError, ValueError):
            pass
        return None

    def _save_owner_pubkey(self, pubkey: str) -> None:
        self._owner_pin_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._owner_pin_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"owner_pubkey": pubkey}).encode())
        finally:
            os.close(fd)
        try:
            os.chmod(self._owner_pin_path, 0o600)
        except OSError:
            pass

    def set_owner_pubkey(self, pubkey: str, rotate: bool = False) -> dict:
        """
        Register the OWNER's public key on this device (Phase 10A). TOFU: the first
        registration pins it; a DIFFERENT key later is rejected unless rotate=True
        (owner key rotation is a deliberate, rare act). Persists across restarts
        (0600, like keys/pins). Once set it populates the node descriptor's
        owner_pubkey slot and enables keyed approval of intervention plans.
        """
        if not isinstance(pubkey, str) or not pubkey:
            return {"status": "error", "code": errors.OWNER_SIG_INVALID,
                    "detail": "owner pubkey must be a non-empty hex string"}
        existing = self.owner_pubkey
        if existing and existing != pubkey and not rotate:
            return {"status": "error", "code": errors.OWNER_KEY_MISMATCH,
                    "detail": "an owner key is already pinned; pass rotate=True to replace it"}
        self._save_owner_pubkey(pubkey)
        self.owner_pubkey = pubkey
        # Reflect the new owner in discovery immediately if the swarm is up.
        try:
            self._publish_node_descriptor()
        except Exception:
            pass
        return {"status": "ok", "owner_fingerprint": "owner:" + crypto.derive_node_id(pubkey),
                "rotated": bool(existing and existing != pubkey)}

    def _prune_owner_seen(self, now: float) -> None:
        stale = [n for n, t in self._owner_approval_seen.items()
                 if now - t > signing.REPLAY_WINDOW_SECONDS]
        for n in stale:
            self._owner_approval_seen.pop(n, None)

    def _verify_owner_approval(self, owner_approval: dict, plan_hash: str) -> dict:
        """
        Verify a keyed owner approval bound to `plan_hash`. Returns a decision dict:
          {"kind":"keyed","approved":True, owner_pubkey, owner_sig, nonce, ts, approver}
          {"kind":"keyed","approved":False, "code":..., "detail":...}
        The signature must be over signing.owner_approval_subject(plan_hash, THIS
        device, nonce, ts) — so it binds to the exact normalized plan, THIS device,
        and a fresh nonce+ts. A signature for a different plan yields a different
        subject and fails verification (no separate plan compare needed).
        """
        def deny(code, detail):
            return {"kind": "keyed", "approved": False, "code": code, "detail": detail}

        if self.owner_pubkey is None:
            return deny(errors.OWNER_UNREGISTERED, "no owner key registered on this device")
        pub   = owner_approval.get("owner_pubkey")
        sig   = owner_approval.get("sig")
        nonce = owner_approval.get("nonce")
        ts    = owner_approval.get("ts")
        if pub != self.owner_pubkey:
            return deny(errors.OWNER_KEY_MISMATCH, "owner signature is not from the pinned owner key")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return deny(errors.OWNER_APPROVAL_STALE, "owner approval has no valid ts")
        now = time.time()
        if abs(now - ts) > signing.REPLAY_WINDOW_SECONDS:
            return deny(errors.OWNER_APPROVAL_STALE, "owner approval outside the replay window")
        if not isinstance(nonce, str) or not nonce:
            return deny(errors.OWNER_APPROVAL_STALE, "owner approval has no nonce")
        self._prune_owner_seen(now)
        if nonce in self._owner_approval_seen:
            return deny(errors.OWNER_APPROVAL_STALE, "owner approval nonce already used (replay)")
        try:
            sig_bytes = bytes.fromhex(sig)
        except (ValueError, TypeError):
            return deny(errors.OWNER_SIG_INVALID, "owner signature is not valid hex")
        subject = signing.owner_approval_subject(plan_hash, self.node_id, nonce, ts)
        if not crypto.verify(subject, sig_bytes, pub):
            return deny(errors.OWNER_SIG_INVALID, "owner signature did not verify over the plan")
        # Accept — record the nonce so this exact approval cannot be replayed.
        self._owner_approval_seen[nonce] = now
        return {"kind": "keyed", "approved": True, "owner_pubkey": pub,
                "owner_sig": sig, "nonce": nonce, "ts": ts,
                "approver": "owner:" + crypto.derive_node_id(pub)}

    def _verify_delegation_approval(self, owner_approval: dict, capability: str,
                                    delegate_agent_id: str, parent_binding_id: str) -> dict:
        """
        Verify a keyed owner approval that sanctions delegating an INTERVENTION-tier
        capability's PROPOSE right to a SPECIFIC agent B (Phase 10B). Same TOFU +
        replay + seen-cache discipline as _verify_owner_approval, but the signed
        subject binds capability + delegate B + parent binding + this device — so A
        cannot launder a mutation right to a B the owner never named. Returns a
        decision dict {approved, code?, detail?, approver?, owner_*}.
        """
        def deny(code, detail):
            return {"approved": False, "code": code, "detail": detail}

        if self.owner_pubkey is None:
            return deny(errors.OWNER_UNREGISTERED,
                        "delegating an intervention right requires a registered owner key")
        if not isinstance(owner_approval, dict):
            return deny(errors.OWNER_APPROVAL_REQUIRED,
                        "intervention-tier delegation requires a keyed owner approval naming the delegate")
        pub   = owner_approval.get("owner_pubkey")
        sig   = owner_approval.get("sig")
        nonce = owner_approval.get("nonce")
        ts    = owner_approval.get("ts")
        if pub != self.owner_pubkey:
            return deny(errors.OWNER_KEY_MISMATCH, "owner signature is not from the pinned owner key")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return deny(errors.OWNER_APPROVAL_STALE, "delegation approval has no valid ts")
        now = time.time()
        if abs(now - ts) > signing.REPLAY_WINDOW_SECONDS:
            return deny(errors.OWNER_APPROVAL_STALE, "delegation approval outside the replay window")
        if not isinstance(nonce, str) or not nonce:
            return deny(errors.OWNER_APPROVAL_STALE, "delegation approval has no nonce")
        self._prune_owner_seen(now)
        if nonce in self._owner_approval_seen:
            return deny(errors.OWNER_APPROVAL_STALE, "delegation approval nonce already used (replay)")
        try:
            sig_bytes = bytes.fromhex(sig)
        except (ValueError, TypeError):
            return deny(errors.OWNER_SIG_INVALID, "delegation approval signature is not valid hex")
        subject = signing.delegation_approval_subject(
            capability, delegate_agent_id, parent_binding_id, self.node_id, nonce, ts)
        if not crypto.verify(subject, sig_bytes, pub):
            return deny(errors.OWNER_SIG_INVALID,
                        "owner signature did not verify over (capability, delegate, parent)")
        self._owner_approval_seen[nonce] = now
        return {"approved": True, "owner_pubkey": pub, "owner_sig": sig,
                "owner_nonce": nonce, "owner_ts": ts,
                "approver": "owner:" + crypto.derive_node_id(pub)}

    def _resolve_plan_approval(self, nplan: dict, plan_hash: str,
                               agent_id: str, message: dict) -> dict:
        """
        THE per-plan approval decision, ONE gate, priority order (no second policy
        surface — only the ACCEPTANCE proof differs):
          1. an attached owner signature  → keyed verify (authoritative; a bad sig
             is a hard deny, never a silent fall-through to the callback);
          2. else a local console callback → Phase 8 behaviour (unchanged);
          3. else an owner key is registered → PENDING: hand the owner the exact
             plan_hash + nonce to sign and resubmit;
          4. else → deny (Phase 8 default).
        """
        owner_approval = message.get("owner_approval")
        if isinstance(owner_approval, dict):
            return self._verify_owner_approval(owner_approval, plan_hash)
        if self._intervention_approval_callback is not None:
            approved = self._intervention_approve(nplan, agent_id)
            return {"kind": "local", "approved": approved, "approver": "device_owner@local"}
        if self.owner_pubkey is not None:
            return {"kind": "pending", "approved": False,
                    "nonce": os.urandom(8).hex(), "ts": time.time()}
        return {"kind": "local", "approved": False, "approver": "device_owner@local"}

    def _audit_log(self) -> AuditLog:
        if self._audit is None:
            self._audit = AuditLog(self.name, self.private_key, self.public_key)
        return self._audit

    def attach_intervention(self, family: str, target: str,
                           name: str | None = None, **opts) -> dict:
        """
        Publish a MUTATING intervention capability on THIS node — the FIX half of
        the fix loop (diagnose → plan → approve → execute → verify → audit).

        DOUBLE GATE (deny-by-default): binding this capability needs owner approval
        (the right to PROPOSE), and every concrete plan needs its own per-plan owner
        approval (set_intervention_approval_callback) before anything executes.

        `family` ∈ INTERVENTION_EXECUTORS; `target` names the subsystem (a systemd
        unit / device node / module). `opts` pass to the executor (e.g. user=True).
        Returns the capability descriptor, or {"error": ...} for an unknown family.
        """
        cls = INTERVENTION_EXECUTORS.get(family)
        if cls is None:
            return {"error": "unknown_intervention_family", "family": family,
                    "known": sorted(INTERVENTION_EXECUTORS)}

        executor      = cls(target, **opts)
        man           = _manifest.intervention_manifest(family, target)
        tier          = man["consent_tier"]                       # "intervention"
        paired_family = _manifest.INTERVENTION_PAIRED_DIAGNOSTIC.get(family, "")
        cap_name      = name or f"intv_{family}_{self._diag_slug(target)}"
        access        = "consent_required"

        live_state = {"family": family, "target": target,
                      "paired_diagnostic": paired_family, "mutating": True,
                      "access": access}

        # Readable through the DEFAULT get_reading path (a proper DataProvider
        # frame), but NOT registered in self._virtual — so the ungated `action`
        # verb cannot reach it. Only propose_intervention can mutate.
        self.data.register_reading_source(cap_name, lambda ls=dict(live_state): dict(ls))

        # BIND-TIME gate (first of the double gate): intervention tier requires
        # owner approval to even hold a propose-lease. Default approval callback
        # DENIES, so a remote agent gets nothing without explicit owner opt-in.
        self.policy.require_approval(cap_name)

        cap = Capability(
            name=cap_name,
            tags=[cap_name, access, "intervention", family],
            live_state=live_state,
            node_id=self.node_id,
            public_key=self.public_key,
            manifest=man,
        )
        self.capabilities[cap_name]   = cap
        self.broker.quotas[cap_name]  = 1
        self._interventions[cap_name] = {
            "executor": executor, "family": family, "target": target,
            "paired_family": paired_family,
        }

        try:
            ip, port = self.swarm.address
            self.swarm.publish(self._capability_record(cap, ip, port))
            self._publish_node_descriptor()                # offered set changed
        except Exception:
            pass

        print(f"[{self.name}] attach_intervention  family={family}  target={target!r}  "
              f"cap={cap_name}  tier={tier}  (double-gated)")
        return {"name": cap_name, "family": family, "target": target,
                "paired_diagnostic": paired_family, "consent_tier": tier,
                "access": access, "node_id": self.node_id}

    def detach_intervention(self, cap_name: str) -> dict:
        """Remove an intervention capability: tears down bindings, drops the
        executor + capability + quota + source, and unpublishes the record."""
        if cap_name not in self._interventions:
            return {"error": "intervention_not_found", "cap_name": cap_name}
        try:
            infos = self.broker.teardown_capability(cap_name, "shutdown")
        except Exception:
            infos = []
        for info in infos:
            try:
                self._cleanup_binding_stream(info["binding_id"])
            except Exception:
                pass
        self._interventions.pop(cap_name, None)
        self.capabilities.pop(cap_name, None)
        self.broker.quotas.pop(cap_name, None)
        self.data._sources.pop(cap_name, None)
        try:
            self.swarm.unpublish({"node_id": self.node_id, "name": cap_name})
            self._publish_node_descriptor()                # offered set changed
        except Exception:
            pass
        print(f"[{self.name}] detach_intervention  cap={cap_name}")
        return {"status": "detached", "cap_name": cap_name}

    # ── plan validation + verify + audit (device-side, never trusted from agent) ─

    def _validate_plan(self, capability: str, plan) -> tuple[bool, str, dict]:
        """
        Structurally validate an InterventionPlan against the capability's manifest
        and its paired diagnostic. Returns (ok, why, normalized_plan). The
        normalized plan is exactly what gets hashed, approved, executed, audited —
        so approval binds to a concrete plan (TOCTOU-free single round).

        Mandatory: action (a manifest mutating action), params (dict), evidence
        (dict), expected (non-empty str), verify {diagnostic, condition} (the
        condition VALIDATED against the paired diagnostic's manifest, no 'changed'),
        reversible (bool). reversible:true REQUIRES a non-empty reversible_how;
        reversible:false REQUIRES reversible_how == "" AND reversible_ack == true
        (an explicit no-undo acknowledgement, surfaced to the owner).
        """
        intv = self._interventions[capability]
        man  = self.capabilities[capability].manifest or {}

        if not isinstance(plan, dict):
            return False, "plan must be an object", {}

        action = plan.get("action")
        actions = man.get("actions", {})
        if action not in actions:
            return False, f"unknown action {action!r}; manifest declares {sorted(actions)}", {}
        if not actions[action].get("mutating"):
            return False, f"action {action!r} is not a mutating action", {}

        params = plan.get("params", {})
        if not isinstance(params, dict):
            return False, "params must be an object", {}

        evidence = plan.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            return False, "evidence (the diagnostic reading justifying the fix) is required", {}

        expected = plan.get("expected")
        if not isinstance(expected, str) or not expected:
            return False, "expected (a statement of the intended outcome) is required", {}

        verify = plan.get("verify")
        if not isinstance(verify, dict):
            return False, "verify {diagnostic, condition} is required", {}
        condition = verify.get("condition")
        if not isinstance(condition, dict):
            return False, "verify.condition is required", {}
        if condition.get("op") == "changed":
            return False, "verify.condition op 'changed' is not a definite predicate", {}
        # Validate the condition against the PAIRED diagnostic's manifest so an
        # agent cannot declare an unverifiable or meaningless check.
        try:
            diag_man = _manifest.diagnostic_manifest(intv["paired_family"], intv["target"])
            norm_cond = conditions.validate_condition(condition, diag_man)
        except _manifest.ManifestError as e:
            return False, f"paired diagnostic unavailable: {e}", {}
        except conditions.ConditionError as e:
            return False, f"invalid verify.condition: {e}", {}

        reversible = plan.get("reversible")
        if not isinstance(reversible, bool):
            return False, "reversible (bool, explicit) is required", {}
        reversible_how = plan.get("reversible_how", "")
        if reversible:
            if not isinstance(reversible_how, str) or not reversible_how:
                return False, "reversible:true requires a non-empty reversible_how", {}
            reversible_ack = False
        else:
            if reversible_how not in ("", None):
                return False, "reversible:false requires reversible_how == \"\"", {}
            reversible_how = ""
            if plan.get("reversible_ack") is not True:
                return False, ("reversible:false requires an explicit reversible_ack:true "
                               "(a no-undo acknowledgement, surfaced to the owner)"), {}
            reversible_ack = True

        normalized = {
            "action":         action,
            "params":         params,
            "evidence":       evidence,
            "expected":       expected,
            "verify":         {"diagnostic": verify.get("diagnostic", intv["paired_family"]),
                               "condition": norm_cond},
            "reversible":     reversible,
            "reversible_how": reversible_how,
            "reversible_ack": reversible_ack,
        }
        return True, "", normalized

    def _run_verify(self, intv: dict, plan: dict) -> dict:
        """
        DEVICE-run post-action verify: read the paired diagnostic FRESH and check
        the plan's declared condition against it. The device NEVER trusts a verify
        result asserted by the agent — it reads real state itself. Returns
        {ran, passed, condition, reading}.
        """
        cond = plan["verify"]["condition"]
        try:
            # Read the paired diagnostic in the SAME scope the executor mutated
            # (e.g. user-scope service), so verify never checks the wrong thing.
            dkw     = intv["executor"].diagnostic_kwargs()
            src     = DIAGNOSTIC_SOURCES[intv["paired_family"]](intv["target"], **dkw)
            reading = src.read() or {}
            passed  = conditions.satisfied(cond, reading)
            return {"ran": True, "passed": bool(passed),
                    "condition": cond, "reading": reading}
        except Exception as e:
            return {"ran": True, "passed": False, "condition": cond,
                    "reading": {"error": f"{type(e).__name__}: {e}"}}

    def _intervention_approve(self, plan: dict, agent_id: str) -> bool:
        cb = self._intervention_approval_callback
        if cb is None:
            return False                                  # default DENY (safe)
        try:
            return bool(cb(plan, agent_id))
        except Exception:
            return False

    def _audit_intervention(self, *, agent_id: str, capability: str, plan: dict,
                            plan_hash: str, approved: bool, executed: bool,
                            verify_outcome: str, result_status: str,
                            verify_reading: dict | None = None,
                            detail: str = "",
                            approval: dict | None = None) -> dict | None:
        """Append ONE signed audit entry for a terminal intervention outcome.
        Returns the signed entry, or None if the log refused (fail-closed).

        `approval` is the resolver's decision (Phase 10A). For a KEYED approval the
        entry records the owner PUBKEY + owner SIGNATURE and the `approver` becomes
        the owner key fingerprint — cryptographic proof of WHICH key approved,
        upgrading the old 'device_owner@local' console attestation. A local-callback
        approval keeps the console-attestation string and adds no owner fields."""
        approver   = "device_owner@local"
        owner_flds: dict = {}
        if approval and approval.get("kind") == "keyed" and approval.get("approved"):
            approver = approval.get("approver", approver)
            owner_flds = {
                "owner_pubkey": approval.get("owner_pubkey"),
                "owner_sig":    approval.get("owner_sig"),
                "owner_nonce":  approval.get("nonce"),
                "owner_ts":     approval.get("ts"),
            }
        try:
            return self._audit_log().append({
                "kind":           "intervention",
                "agent_id":       agent_id,
                "device_node_id": self.node_id,
                "capability":     capability,
                "plan":           plan,
                "plan_hash":      plan_hash,
                "approved":       approved,
                "approver":       approver,            # owner key fp (keyed) or local attestation
                **owner_flds,
                "executed":       executed,
                "verify_outcome": verify_outcome,          # pass | fail | not_run
                "verify_reading": verify_reading or {},
                "result_status":  result_status,
                "reversible":     plan.get("reversible"),
                "reversible_how": plan.get("reversible_how", ""),
                "ts":             time.time(),
            })
        except AuditError:
            return None

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
        self._publish_node_descriptor()                    # offered set changed
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
            self._publish_node_descriptor()                # offered set changed
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
