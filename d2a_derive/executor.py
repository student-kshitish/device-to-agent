"""
d2a_derive/executor.py — Phase 2: turn a Phase-1 DerivationPlan into a LIVE
DerivedCapability.

The plan is the seam. Phase 1 stopped at "here is a bindable plan": which recipe,
which provider satisfies each `requires` input, the effective consent tier, the
provenance. Phase 2 makes it move:

    plan  ──▶  DerivedCapability
                 ├─ binds every required input via RemoteAgent under a REAL lease
                 │    (auto-renewed; works over LAN or DHT — whatever swarm the
                 │     agent holds, the executor never looks)
                 ├─ feeds each input to the recipe's transform:
                 │    subscribe (streaming providers) OR a bounded pull loop,
                 │    per the provider manifest's `streaming` flag
                 ├─ per input frame: RESOLVE the recipe's dotted fields out of the
                 │    device frame's `raw` (the DataProvider flatten convention),
                 │    APPLY the declared unit scale factor, hand transform.on_frame
                 │    a normalized {"input", "fields", "ts", "seq"} frame
                 └─ exposes the running transform output through .reading()

reading() returns None until the transform first emits; after that it always
returns the latest output, and health()["last_output_ts"] carries when.

PHASE 3 (v1.5): publish() registers a live DerivedCapability on a DeviceRuntime so
OTHER agents can discover/bind/read/subscribe to it — through the same
_register_virtual path a Guardian VSO uses. Consent gates on the effective tier
(no bypass); a `failed` derivation unpublishes + tears down consumer bindings with
a distinct `derived_input_failed` code; a `degraded` one keeps serving with its
state in the reading envelope. See DerivedCapability.publish.

SELF-HEALING (healer.py) and STALENESS (monitor.py) are wired in here but live in
their own modules: the executor owns the shared per-input state they read/write
(the ONE lock discipline below), so a lease loss or a stale input flips
DerivedCapability state without either module reaching into a network call.

LOCK DISCIPLINE (load-bearing): `self._lock` guards ALL shared state — the
transform ctx, the latest output, and every InputFeed's mutable fields. NETWORK
calls (bind / subscribe / request_data / release) NEVER run under the lock, so the
agent's renew thread firing on_lease_lost can always take the lock to dispatch a
loss while a heal thread is mid-rebind on the network. Acquire the lock only to
mutate state; drop it before touching the wire.
"""

import threading
import time
from dataclasses import dataclass, field

from agents.remote_agent import LeaseLostError
from d2a import errors as _wire_errors
from d2a import manifest as _manifest
from d2a_derive import units
from d2a_derive.metrics import MetricsStore
from d2a_derive.validator import DERIVE_MAX_INPUT_HZ

# DerivedCapability lifecycle states (overall). STARTING is transient during
# start(); the steady states are the three the recipe's health contract cares
# about, plus CLOSED after a clean shutdown.
STARTING = "starting"
ACTIVE   = "active"
DEGRADED = "degraded"
FAILED   = "failed"
CLOSED   = "closed"

# Per-input feed states. "gone" is terminal for that input (healer gave up);
# whether it takes the whole capability to FAILED or only DEGRADED depends on
# whether the recipe marked the input optional.
IN_ACTIVE    = "active"
IN_DEGRADED  = "degraded"     # stale, or rebinding — present but not healthy
IN_REBINDING = "rebinding"
IN_GONE      = "gone"

# A recipe field's min_hz is a FLOOR, not a target — feeding the transform faster
# than the minimum is always fine (these transforms are incremental) and makes the
# derivation more responsive. We aim above the floor at a sensible default per feed
# mode, then clamp to the device cadence ceiling (an agent can never receive frames
# faster than the device reads).
_DEFAULT_STREAM_HZ = 5.0
_DEFAULT_PULL_HZ   = 2.0


def _resolve_dotted(raw: dict, dotted: str):
    """Resolve a dotted field name against a device frame's `raw` dict, exactly
    the way DataProvider/Preprocessor flattens it (source.field → nested dict).
    Returns the value, or None if any path segment is missing. Never raises."""
    node = raw
    for seg in dotted.split("."):
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


@dataclass
class InputFeed:
    """One live input of a derived capability: a bound provider whose frames are
    resolved, scaled, and pushed into the transform. Mutable fields are guarded by
    the owning DerivedCapability's lock (see module lock discipline)."""
    hint: str                       # the requires capability_hint (== the cap name)
    provider: dict                  # discovered provider {node_id, name, manifest}
    fields: list                    # recipe-declared dotted field names for this input
    scales: dict                    # dotted field -> multiplicative unit scale factor
    optional: bool                  # recipe marked this input optional?
    streaming: bool                 # provider manifest streaming? (subscribe vs pull)
    feed_hz: float                  # subscribe/pull cadence (clamped)
    min_interval_s: float           # 1/feed_hz — the "expected" gap for staleness

    binding: dict | None = None     # current lease binding dict
    state: str = IN_ACTIVE
    last_frame_ts: float = 0.0      # wall-clock of the last frame we ingested
    last_seq: int = -1              # last provider frame seq (gap detection)
    gap_count: int = 0
    rebind_count: int = 0

    # CHAINED input (local multi-hop): this input is produced by an inner
    # DerivedCapability instantiated locally, not bound from the wire. When
    # is_inner is True, `inner_plan`/`inner` are set and `provider`/`binding` are not
    # used — the feed reads inner.reading() and mirrors the inner's health outward.
    is_inner: bool = False
    inner_plan: object = None
    inner: object = None            # the live inner DerivedCapability

    # feed machinery
    _stop: threading.Event = field(default_factory=threading.Event)
    _pull_thread: object = None
    _healing: bool = False          # a heal is in flight for this input
    _inner_seq: int = 0             # monotonic seq for locally generated inner frames


class DerivedCapability:
    """
    A live, running derivation. Construct from a Phase-1 plan + a started
    RemoteAgent, call start(), then read the derived output with reading(). Health
    (state + per-input telemetry) is available any time via health(). close()
    releases every input binding and leaves zero device-side residue.

    on_state_change(old_state, new_state, reason) fires on every overall-state
    transition (active/degraded/failed) so a caller can react to a required input
    failing or a stale input recovering.
    """

    def __init__(self, plan, agent, *,
                 staleness_factor: float = 3.0,
                 heal_max_attempts: int = 4,
                 heal_backoff_s: float = 0.2,
                 heal_shutdown_backoff_s: float = 2.0,
                 monitor_interval_s: float | None = None,
                 on_state_change=None,
                 metrics: MetricsStore | None = None):
        self.plan = plan
        self.agent = agent
        self.provided_name = plan.provided_name
        self.recipe = plan.recipe
        self.module = plan.recipe.module         # the loaded transform
        self.effective_tier = plan.effective_tier
        self.manifest = plan.manifest
        self.provenance = plan.provenance
        self.on_state_change = on_state_change

        self._lock = threading.RLock()
        self._ctx: dict = {}                     # the transform's single ctx
        self._latest = None                      # latest transform output (or None)
        self._last_output_ts = 0.0
        self._state = STARTING
        self._closed = False

        # Phase 6 metrics: in-memory accumulators updated at transitions (heal count
        # is read from feed.rebind_count at flush; staleness is sampled by the monitor
        # into these sums). Persisted to disk ONCE per run — no per-frame writes.
        self._metrics = metrics if metrics is not None else MetricsStore()
        self._start_ts = 0.0
        self._staleness_sum = 0.0                # Σ per-tick max input staleness
        self._staleness_n = 0                    # tick count (for the run's mean)
        self._metrics_recorded = False           # guard: record the run exactly once

        self._feeds: list[InputFeed] = self._build_feeds()

        # set when this derived capability is published on-wire (publish()).
        self._publisher = None
        self._published_name = None

        # self-healing + staleness monitor — own modules, share our state + lock.
        # Imported here (not at module top) to keep the import graph acyclic and
        # obvious: executor owns them, they never import executor.
        from d2a_derive.healer import SelfHealer
        from d2a_derive.monitor import StalenessMonitor
        self._healer = SelfHealer(
            self,
            max_attempts=heal_max_attempts,
            backoff_s=heal_backoff_s,
            shutdown_backoff_s=heal_shutdown_backoff_s,
        )
        self._monitor = StalenessMonitor(
            self,
            staleness_factor=staleness_factor,
            interval_s=monitor_interval_s,
        )

    # ── construction ────────────────────────────────────────────────────────────

    def _build_feeds(self) -> list[InputFeed]:
        """Turn plan.inputs (+ the recipe's requires, for optional/unit info) into
        InputFeed objects with resolved dotted fields and unit scale factors."""
        # index the recipe's requires by hint so we can read optional + declared
        # units without touching Phase-1's planner (which stays committed as-is).
        req_by_hint: dict[str, dict] = {}
        for req in self.recipe.requires:
            req_by_hint.setdefault(req.get("capability_hint"), req)

        feeds = []
        for m in self.plan.inputs:
            hint = m["hint"]
            inner_plan = m.get("inner_plan")            # chained input (local hop)?
            provider = m.get("provider") or {}
            # the manifest that describes THIS input's fields+units: the discovered
            # provider's, or (chained) the inner plan's own provides manifest.
            src_manifest = inner_plan.manifest if inner_plan is not None \
                else (provider.get("manifest", {}) or {})
            req = req_by_hint.get(hint, {})
            req_fields = req.get("fields", {})
            src_reading = src_manifest.get("reading", {})

            dotted = sorted(req_fields)
            scales = {}
            min_hz = 0.0
            for fname, spec in req_fields.items():
                req_unit  = spec.get("unit")
                prov_unit = (src_reading.get(fname, {}) or {}).get("unit")
                # identity when units match or none declared; else the declared
                # multiplicative scale the contract already proved is supported.
                scales[fname] = units.scale_factor(prov_unit or req_unit or "",
                                                   req_unit or prov_unit or "") or 1.0
                mh = spec.get("min_hz")
                if isinstance(mh, (int, float)) and not isinstance(mh, bool):
                    min_hz = max(min_hz, float(mh))

            streaming = bool(src_manifest.get("streaming"))
            target_hz = max(min_hz, _DEFAULT_STREAM_HZ if streaming else _DEFAULT_PULL_HZ)
            feed_hz = min(max(target_hz, 0.1), DERIVE_MAX_INPUT_HZ)
            feeds.append(InputFeed(
                hint=hint, provider=provider, fields=dotted, scales=scales,
                optional=bool(req.get("optional", False)),
                streaming=streaming, feed_hz=feed_hz, min_interval_s=1.0 / feed_hz,
                is_inner=inner_plan is not None, inner_plan=inner_plan,
            ))
        return feeds

    # ── lifecycle ───────────────────────────────────────────────────────────────

    def start(self) -> "DerivedCapability":
        """Bind every input under a lease, start its feed, and start the monitor.
        A required input that will not bind raises RuntimeError (the plan promised
        it was satisfiable — a bind failure here is a real, surfaced error); an
        optional input that will not bind starts the capability DEGRADED."""
        # chain onto the agent's lease-loss hook so we're told the moment any of
        # our input leases dies (renew denied / lease_expired / device_shutdown).
        self._install_lease_hook()
        self._start_ts = time.time()
        self.module.init(self._ctx)

        for feed in self._feeds:
            if feed.is_inner:
                ok = self._start_inner(feed)          # local chained hop
            else:
                ok = self._bind_feed(feed)            # bind a real/published provider
                if ok:
                    self._start_feed(feed)
            if not ok:
                if feed.optional:
                    with self._lock:
                        feed.state = IN_GONE
                    continue
                # a required input the plan said was satisfiable failed to come up —
                # tear down what we started and surface it.
                self.close()
                raise RuntimeError(
                    f"derived '{self.provided_name}': required input '{feed.hint}' "
                    f"failed to start")

        self._recompute_state("started")
        self._monitor.start()
        return self

    # ── chained inputs (local multi-hop) ─────────────────────────────────────────

    def _start_inner(self, feed: InputFeed) -> bool:
        """Instantiate + start the inner DerivedCapability that produces a CHAINED
        input, wire its health outward, and begin feeding its readings to our
        transform. The inner runs its OWN executor (binds its own inputs, self-heals,
        monitors) — we only mirror its overall state onto this feed."""
        feed.inner = DerivedCapability(
            feed.inner_plan, self.agent,
            on_state_change=self._make_inner_mirror(feed),
        )
        try:
            feed.inner.start()
        except Exception:
            return False
        with self._lock:
            feed.state = IN_ACTIVE
            feed.last_frame_ts = time.time()
        feed._stop.clear()
        t = threading.Thread(target=self._inner_feed_loop, args=(feed,), daemon=True,
                             name=f"derive-inner-{feed.hint}")
        feed._pull_thread = t
        t.start()
        return True

    def _make_inner_mirror(self, feed: InputFeed):
        """Mirror the inner derivation's overall state onto this outer feed, so
        inner failure/degradation propagates outward through the SAME state machine:
        inner failed → outer input gone (→ outer failed/degraded per required/
        optional); inner degraded → outer feed degraded; inner active → active."""
        def _mirror(old, new, reason):
            with self._lock:
                if self._closed:
                    return
                if new == FAILED:
                    feed.state = IN_GONE
                elif new == DEGRADED:
                    feed.state = IN_DEGRADED
                elif new == ACTIVE:
                    feed.state = IN_ACTIVE
                self._recompute_state_locked(f"inner:{feed.hint}:{new}")
        return _mirror

    def _inner_feed_loop(self, feed: InputFeed) -> None:
        """Poll the inner derivation's reading() and feed it to our transform. The
        inner reading is a flat dict whose keys are the recipe's seam fields."""
        while not feed._stop.is_set():
            out = feed.inner.reading() if feed.inner is not None else None
            if isinstance(out, dict):
                feed._inner_seq += 1
                self._ingest(feed, {"raw": dict(out), "ts": time.time(),
                                    "seq": feed._inner_seq})
            if feed._stop.wait(feed.min_interval_s):
                return

    def _bind_feed(self, feed: InputFeed) -> bool:
        """Bind one input to its planned provider under a lease. NETWORK op — no
        lock held. Returns True and records feed.binding on success."""
        node_id = feed.provider.get("node_id")
        resp = self.agent.bind_remote_to(node_id, feed.hint)
        if not resp.get("verified"):
            return False
        with self._lock:
            feed.binding = resp
            feed.state = IN_ACTIVE
            feed.last_frame_ts = time.time()
            feed.last_seq = -1
        return True

    def _start_feed(self, feed: InputFeed) -> None:
        """Begin delivering this input's frames to the transform: subscribe if the
        provider streams, else a bounded pull loop at feed_hz."""
        feed._stop.clear()
        if feed.streaming:
            self.agent.start_stream(feed.binding, self._make_frame_cb(feed), hz=feed.feed_hz)
        else:
            t = threading.Thread(target=self._pull_loop, args=(feed,), daemon=True,
                                 name=f"derive-pull-{feed.hint}")
            feed._pull_thread = t
            t.start()

    def _make_frame_cb(self, feed: InputFeed):
        def _cb(inner_frame: dict) -> None:
            self._ingest(feed, inner_frame)
        return _cb

    def _pull_loop(self, feed: InputFeed) -> None:
        """Bounded pull feed for a non-streaming provider. Sleeps between reads
        (never busy-spins); a lease loss surfaces as LeaseLostError and is routed
        to the healer, then the loop exits (the heal thread restarts the feed)."""
        while not feed._stop.is_set():
            try:
                resp = self.agent.request_data(feed.binding, feed.hint)
            except LeaseLostError as e:
                self._healer.on_loss(feed, e.code)
                return
            if isinstance(resp, dict) and resp.get("type") == "reading":
                self._ingest(feed, resp.get("frame", {}))
            if feed._stop.wait(feed.min_interval_s):
                return

    # ── the feed → transform path ───────────────────────────────────────────────

    def _ingest(self, feed: InputFeed, inner_frame: dict, _resync: bool = False) -> None:
        """Resolve the recipe's declared fields out of one device frame, apply the
        declared unit scale, and drive transform.on_frame. Detects a provider seq
        gap and triggers exactly one resync re-read (never silent)."""
        if not isinstance(inner_frame, dict):
            return
        # Two frame shapes reach here: a hardware DataProvider frame
        # {"raw": {...}, "derived": {...}, "seq": ...}, and a VIRTUAL capability's
        # flat reading dict {field: value} (a published derived / VSO / emergent
        # provider serves this — get_reading returns reading_fn() directly, with no
        # "raw" wrapper). Resolve dotted fields against whichever we got.
        raw = inner_frame.get("raw") if "raw" in inner_frame else inner_frame
        seq = inner_frame.get("seq")

        fields = {}
        for dotted in feed.fields:
            val = _resolve_dotted(raw, dotted)
            # numeric fields are unit-scaled; boolean/string fields pass through
            # unchanged (a CHAINED input's seam fields — e.g. presence.in_use, a
            # trend string — are not numbers, and dropping them would starve the
            # outer transform). None (absent this frame) and dict/list are skipped.
            if isinstance(val, bool):
                fields[dotted] = val
            elif isinstance(val, (int, float)):
                fields[dotted] = val * feed.scales.get(dotted, 1.0)
            elif isinstance(val, str):
                fields[dotted] = val

        gap = 0
        with self._lock:
            if self._closed:
                return
            # gap detection: the device Preprocessor seq is monotonic per
            # capability. A jump (or an explicit _gap from the event channel) means
            # frames were missed. Only on a live (non-resync) frame.
            if not _resync and isinstance(seq, int):
                explicit = inner_frame.get("_gap")
                if isinstance(explicit, int) and explicit > 0:
                    gap = explicit
                elif feed.last_seq >= 0 and seq > feed.last_seq + 1:
                    gap = seq - feed.last_seq - 1
                feed.last_seq = seq
            feed.last_frame_ts = time.time()
            # staleness recovery: a fresh frame on a degraded-by-staleness input
            # flips it back to active (handled centrally in _recompute_state).
            # staleness recovery flips a degraded DIRECT feed back to active on a
            # fresh frame. An INNER (chained) feed's state is owned by the mirror of
            # the inner derivation — never auto-recover it here, or a stale inner
            # reading would falsely promote it.
            if feed.state == IN_DEGRADED and not feed._healing and not feed.is_inner:
                feed.state = IN_ACTIVE

            if fields:
                frame = {"input": feed.hint, "fields": fields,
                         "ts": inner_frame.get("ts", time.time()), "seq": seq}
                try:
                    out = self.module.on_frame(feed.hint, frame, self._ctx)
                except Exception:
                    out = None
                if isinstance(out, dict):
                    self._latest = out
                    self._last_output_ts = time.time()
            self._recompute_state_locked("frame")

        if gap > 0:
            feed.gap_count += 1
            print(f"[derive:{self.provided_name}] input '{feed.hint}' gap={gap} "
                  f"(count={feed.gap_count}) → one resync re-read")
            self._resync(feed)

    def _resync(self, feed: InputFeed) -> None:
        """Exactly one fresh get_reading to fill a detected gap. NETWORK op — no
        lock. A lease loss during resync routes to the healer."""
        try:
            resp = self.agent.request_data(feed.binding, feed.hint)
        except LeaseLostError as e:
            self._healer.on_loss(feed, e.code)
            return
        if isinstance(resp, dict) and resp.get("type") == "reading":
            self._ingest(feed, resp.get("frame", {}), _resync=True)

    # ── reading + health ────────────────────────────────────────────────────────

    def reading(self):
        """The latest transform output, or None until the transform first emits."""
        with self._lock:
            return self._latest

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def health(self) -> dict:
        """Snapshot: {state, per_input:{hint:{state, staleness_s, gap_count,
        rebind_count}}, last_output_ts}."""
        now = time.time()
        with self._lock:
            per_input = {}
            for feed in self._feeds:
                staleness = (now - feed.last_frame_ts) if feed.last_frame_ts else None
                entry = {
                    "state":        feed.state,
                    "staleness_s":  round(staleness, 3) if staleness is not None else None,
                    "gap_count":    feed.gap_count,
                    "rebind_count": feed.rebind_count,
                }
                # a chained input nests the inner derivation's own health, so the
                # full multi-hop health tree is readable from the top.
                if feed.is_inner and feed.inner is not None:
                    entry["inner"] = feed.inner.health()
                per_input[feed.hint] = entry
            return {
                "state":          self._state,
                "per_input":      per_input,
                "last_output_ts": self._last_output_ts or None,
                # Phase 6: the recipe's lifetime metrics on THIS machine (rolling over
                # all prior runs, not just this one) — the same record the planner
                # ranks by and explain() prints. Advisory; measures local history.
                "lifetime":       self._metrics.get(self.recipe.recipe_name).summary(),
            }

    def _note_staleness_locked(self, sample: float) -> None:
        """Fold one staleness observation into this run's accumulators (caller holds
        the lock). Called by the monitor each tick — in memory only, never touches
        disk, so the no-per-frame-write bound holds."""
        if self._closed:
            return
        self._staleness_sum += max(0.0, float(sample))
        self._staleness_n += 1

    # ── state machine ─────────────────────────────────────────────────────────────

    def _recompute_state(self, reason: str) -> None:
        with self._lock:
            self._recompute_state_locked(reason)

    def _recompute_state_locked(self, reason: str) -> None:
        """Fold per-input states into the overall state (caller holds the lock):
        a GONE required input → FAILED; a GONE optional input or any degraded/
        rebinding input → DEGRADED; otherwise ACTIVE. Fires on_state_change on a
        real transition."""
        if self._closed:
            return
        new = ACTIVE
        for feed in self._feeds:
            if feed.state == IN_GONE:
                if not feed.optional:
                    new = FAILED
                    break
                new = DEGRADED
            elif feed.state in (IN_DEGRADED, IN_REBINDING) and new != FAILED:
                new = DEGRADED
        self._transition_locked(new, reason)

    def _transition_locked(self, new: str, reason: str) -> None:
        old = self._state
        if new == old:
            return
        self._state = new
        # Phase 6: FAILED is effectively terminal (it comes from a required input
        # marked gone), so persist the run's metrics AT the transition — durability
        # for the failed_count even if close() is never called. Off the lock (the
        # store does disk IO); the once-guard dedupes against a later close().
        if new == FAILED:
            threading.Thread(target=self._record_run_once, args=(FAILED,),
                             daemon=True).start()
        cb = self.on_state_change
        if cb is not None:
            # fire outside the lock so a callback can't deadlock on our state
            threading.Thread(target=self._fire_state_change, args=(cb, old, new, reason),
                             daemon=True).start()

    @staticmethod
    def _fire_state_change(cb, old, new, reason):
        try:
            cb(old, new, reason)
        except Exception:
            pass

    # ── metrics flush (once per run) ──────────────────────────────────────────────

    def _record_run_once(self, final_state: str) -> None:
        """Persist this run's contribution to the recipe's rolling metrics — EXACTLY
        once per DerivedCapability lifetime (guarded), at the FAILED transition or at
        close(), whichever fires first. heal_count is read from the feeds' rebind
        counters; mean staleness is this run's mean of the monitor's per-tick samples.
        The disk write happens here (off any per-frame path)."""
        with self._lock:
            if self._metrics_recorded or self._start_ts == 0.0:
                return                       # never started, or already recorded
            self._metrics_recorded = True
            uptime = max(0.0, time.time() - self._start_ts)
            heals = sum(f.rebind_count for f in self._feeds)
            mean_staleness = (self._staleness_sum / self._staleness_n) \
                if self._staleness_n else 0.0
            failed = (final_state == FAILED)
            recipe_name = self.recipe.recipe_name
        try:
            self._metrics.record_run(recipe_name, uptime=uptime, heal_count=heals,
                                     failed=failed, staleness=mean_staleness)
        except Exception:                    # advisory — a metrics write must not raise
            pass

    # ── lease-loss hook plumbing (feeds the healer) ──────────────────────────────

    def _install_lease_hook(self) -> None:
        """Chain onto agent.on_lease_lost so multiple DerivedCapabilities can share
        one agent: dispatch a loss to the owning feed, else defer to whatever hook
        was there before us."""
        prev = self.agent.on_lease_lost

        def _hook(binding_id, code, _prev=prev):
            feed = self._feed_for_binding(binding_id)
            if feed is not None:
                self._healer.on_loss(feed, code)
            elif callable(_prev):
                _prev(binding_id, code)

        self.agent.on_lease_lost = _hook

    def _feed_for_binding(self, binding_id: str) -> InputFeed | None:
        with self._lock:
            for feed in self._feeds:
                if feed.binding and feed.binding.get("binding_id") == binding_id:
                    return feed
        return None

    # ── publish on-wire (v1.5) ────────────────────────────────────────────────────

    def publish(self, runtime, name: str | None = None) -> dict:
        """
        Publish this live derived capability on `runtime` (a DeviceRuntime) so OTHER
        agents can discover, bind, read, and subscribe to it — through the EXACT
        same machinery a Guardian VSO / emergent device uses (`_register_virtual`):
        broker quota, a policy rule from the EFFECTIVE consent tier (sensitive →
        require_approval, no bypass), leases, condition-events, unified teardown.

        The published record carries the v1.5 derived-provenance manifest (derived/
        recipe/fidelity/cannot_detect), signed with the runtime's HOST key. reading()
        routes to THIS live DerivedCapability via the pseudo-source registration, so
        a remote subscriber's condition on a derived reading field works.

        LIFECYCLE COUPLING: if the underlying derivation later enters `failed`, the
        published capability is retracted and its consumer bindings are torn down
        with a distinct `derived_input_failed` death code (wired here onto
        on_state_change). A `degraded` derivation keeps serving, with the live state
        exposed in the reading envelope's `derived_state` field.
        """
        name = name or self.provided_name
        man = {**self.manifest, "consent_tier": self.effective_tier}
        # defensive: the published manifest must be a valid v1.5 derived manifest.
        man = _manifest.validate_manifest(man, self.effective_tier)

        self._publisher = runtime
        self._published_name = name

        # chain the failure→retract hook onto whatever on_state_change was set.
        prev = self.on_state_change

        def _coupling(old, new, why, _prev=prev, _rt=runtime, _nm=name):
            if new == FAILED:
                try:
                    _rt.unpublish_derived(_nm, _wire_errors.DERIVED_INPUT_FAILED)
                except Exception:
                    pass
            if callable(_prev):
                _prev(old, new, why)

        self.on_state_change = _coupling

        return runtime._register_virtual(
            name, "derived", self.effective_tier, man,
            tags=[name, "derived_capability", self.effective_tier],
            live_state={"derived": True, "recipe": self.recipe.recipe_name,
                        "state": self.state},
            reading_fn=self._published_reading,
            action_fn=lambda action, params: {"error": "derived_capability_has_no_actions"},
        )

    def _published_reading(self):
        """The reading a remote consumer receives: the latest transform output with
        the live `derived_state` folded into the envelope (so a `degraded`
        derivation is honestly labelled while it keeps serving). None until the
        transform first emits."""
        out = self.reading()
        if out is None:
            return None
        return {**out, "derived_state": self.state}

    # ── shutdown ─────────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Release every input binding and stop all background work. Idempotent.
        After this the device shows zero active binds/subs for our inputs."""
        with self._lock:
            if self._closed:
                return
            prev_state = self._state             # the run's final state, for metrics
            self._closed = True
            self._state = CLOSED
            feeds = list(self._feeds)
            publisher, pub_name = self._publisher, self._published_name

        # if we were published on-wire, retract the record + tear down consumer
        # bindings gracefully (device_shutdown class — the service is intentionally
        # stopping, distinct from the failure code).
        if publisher is not None and pub_name is not None:
            try:
                publisher.unpublish_derived(pub_name, _wire_errors.DEVICE_SHUTDOWN)
            except Exception:
                pass

        self._monitor.stop()
        self._healer.stop()
        for feed in feeds:
            feed._stop.set()
        for feed in feeds:
            t = feed._pull_thread
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=2.0)
        # tear down chained inner derivations (each releases ITS own inputs).
        for feed in feeds:
            if feed.inner is not None:
                try:
                    feed.inner.close()
                except Exception:
                    pass
        # release every direct binding (stops auto-renew, tears down device-side stream)
        for feed in feeds:
            if feed.binding is None:
                continue
            try:
                if feed.streaming:
                    self.agent.stop_stream(feed.binding)
            except Exception:
                pass
            try:
                self.agent.release_binding(feed.binding)
            except Exception:
                pass

        # Phase 6: record this run's metrics (once-guarded — a FAILED transition may
        # already have flushed it). Uses prev_state so a run that closed while FAILED
        # still counts as a failure; a clean close counts as a non-failed run.
        self._record_run_once(prev_state)
