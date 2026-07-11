import threading
import time
from dataclasses import dataclass

from d2a.schema import BindToken, Binding
from d2a.verbs import make_bind_request, make_bind_token, make_binding
from d2a import errors

LEASE_TTL_DEFAULT = 300   # seconds — matches make_bind_token's default token TTL


@dataclass
class ActiveBind:
    """
    One occupied capability slot. Does NOT carry an authoritative token copy —
    the single source of truth for a binding's token (and therefore its lease
    expiry) is broker.bindings[binding_id].token. `token()` reads it from there.
    """
    agent_id: str
    capability_name: str
    priority: int
    bound_at: float
    needs: list[str]
    binding_id: str = ""


class CapabilityBroker:
    """
    Contention broker: quota · priority · preemption · waitqueue · leases.

    Teardown is UNIFIED: explicit release, preemption, and lease expiry all pass
    through _remove_active_bind(reason) so the broker can never end up in an
    inconsistent state via one path but not another. `reason` ∈
    {released, preempted, expired} is recorded on the Binding (status +
    release_reason). Only release and expiry then pull the next queued agent via
    _grant_from_waitqueue; preemption re-queues its victim instead.

    All mutating methods hold self._lock (RLock) so the device's expiry sweeper
    thread and TCP handler threads cannot corrupt shared state.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.active_binds: dict[str, list[ActiveBind]] = {}
        self.quotas: dict[str, int] = {}
        # waitqueue entries: (priority, agent_id, needs)
        self.waitqueue: dict[str, list] = {}
        self.bindings: dict[str, Binding] = {}
        self.bind_history: list = []
        self._lock = threading.RLock()

    def _log(self, event: str, **kwargs):
        self.bind_history.append({"time": time.time(), "event": event, **kwargs})

    def _lease_ttl(self) -> int:
        return int(getattr(self.runtime, "lease_ttl", LEASE_TTL_DEFAULT))

    def _token_of(self, binding_id: str) -> BindToken | None:
        b = self.bindings.get(binding_id)
        return b.token if b else None

    # ── shared teardown primitives ─────────────────────────────────────────────

    def _issue_token(self, agent_id: str, capability_name: str, needs: list[str], priority: int) -> ActiveBind:
        req = make_bind_request(agent_id, capability_name, needs, priority)
        token = make_bind_token(req, self.runtime.node_id, self.runtime.private_key,
                                self.runtime.public_key, self._lease_ttl())
        binding = make_binding(token)
        self.bindings[binding.binding_id] = binding
        return ActiveBind(
            agent_id=agent_id,
            capability_name=capability_name,
            priority=priority,
            bound_at=time.time(),
            needs=needs,
            binding_id=binding.binding_id,
        )

    def _remove_active_bind(self, bind: "ActiveBind", capability_name: str, reason: str) -> None:
        """
        THE ONE teardown codepath. Removes `bind` from the active set and records
        `reason` on its Binding. Does NOT grant the waitqueue — callers decide.
        reason ∈ {released, preempted, expired, shutdown}.
        """
        active = self.active_binds.get(capability_name, [])
        if bind in active:
            active.remove(bind)
        b = self.bindings.get(bind.binding_id)
        if b is not None:
            b.status = reason               # released | preempted | expired
            b.release_reason = reason
        self._log(reason, agent_id=bind.agent_id, capability=capability_name)

    def _grant_from_waitqueue(self, capability_name: str) -> dict | None:
        """Shared: pop the next queued agent (if any) and grant it the freed slot."""
        wq = self.waitqueue.get(capability_name, [])
        if not wq:
            return None
        active = self.active_binds.setdefault(capability_name, [])
        next_priority, next_agent_id, next_needs = wq.pop(0)
        new_bind = self._issue_token(next_agent_id, capability_name, next_needs, next_priority)
        active.append(new_bind)
        self._log("auto_granted", agent_id=next_agent_id, capability=capability_name)
        return {
            "next_agent_id": next_agent_id,
            "token": self._token_of(new_bind.binding_id),
            "binding_id": new_bind.binding_id,
        }

    # ── request / release ──────────────────────────────────────────────────────

    def request_bind(self, agent_id: str, capability_name: str, needs: list[str], priority: int = 5) -> dict:
        with self._lock:
            if self.runtime.get_capability(capability_name) is None:
                return {"status": "error", "code": errors.CAPABILITY_NOT_FOUND,
                        "detail": f"Capability '{capability_name}' not found"}

            quota = self.quotas.get(capability_name, 1)
            active = self.active_binds.setdefault(capability_name, [])
            wq = self.waitqueue.setdefault(capability_name, [])

            if len(active) < quota:
                bind = self._issue_token(agent_id, capability_name, needs, priority)
                active.append(bind)
                self._log("granted", agent_id=agent_id, capability=capability_name, priority=priority)
                return {
                    "status": "granted",
                    "token": self._token_of(bind.binding_id),
                    "binding_id": bind.binding_id,
                    "message": f"Bound to {capability_name}",
                }

            # slot full — check for preemption candidate (higher number = lower priority)
            worst = max(active, key=lambda b: b.priority)
            if worst.priority > priority:
                # Preemption goes through the SAME teardown path as release/expiry,
                # then re-queues the victim (rather than granting the waitqueue).
                self._remove_active_bind(worst, capability_name, "preempted")
                wq.append((worst.priority, worst.agent_id, worst.needs))
                wq.sort(key=lambda x: x[0])

                bind = self._issue_token(agent_id, capability_name, needs, priority)
                active.append(bind)
                self._log("granted_by_preemption", agent_id=agent_id, capability=capability_name, priority=priority)
                return {
                    "status": "granted_by_preemption",
                    "token": self._token_of(bind.binding_id),
                    "binding_id": bind.binding_id,
                    "message": f"Preempted {worst.agent_id}",
                    "preempted_agent_id": worst.agent_id,
                }

            # queue
            wq.append((priority, agent_id, needs))
            wq.sort(key=lambda x: x[0])
            queue_position = next(i + 1 for i, e in enumerate(wq) if e[1] == agent_id)
            self._log("queued", agent_id=agent_id, capability=capability_name, priority=priority, position=queue_position)
            return {"status": "queued", "message": f"Queued at position {queue_position}", "queue_position": queue_position}

    def release_bind(self, agent_id: str, capability_name: str) -> dict:
        with self._lock:
            active = self.active_binds.get(capability_name, [])
            bind = next((b for b in active if b.agent_id == agent_id), None)
            if bind is None:
                return {"status": "error", "code": errors.NO_ACTIVE_BIND,
                        "detail": f"No active bind for agent {agent_id} on {capability_name}"}

            self._remove_active_bind(bind, capability_name, "released")
            grant = self._grant_from_waitqueue(capability_name)
            if grant:
                return {"status": "released", **grant}
            return {"status": "released", "next_agent_id": None}

    # ── lease expiry (shared path) ─────────────────────────────────────────────

    def expire_binding(self, binding_id: str) -> dict | None:
        """
        Expire ONE active binding through the shared teardown path — identical
        removal + waitqueue-grant to release_bind, only the reason differs.
        Returns {capability_name, agent_id, next_agent_id, grant} or None if the
        binding is not currently an active slot holder.
        """
        with self._lock:
            for cap, binds in self.active_binds.items():
                bind = next((b for b in binds if b.binding_id == binding_id), None)
                if bind is not None:
                    agent_id = bind.agent_id
                    self._remove_active_bind(bind, cap, "expired")
                    grant = self._grant_from_waitqueue(cap)
                    return {
                        "capability_name": cap,
                        "agent_id": agent_id,
                        "next_agent_id": grant["next_agent_id"] if grant else None,
                        "grant": grant,
                    }
            return None

    def sweep_expired(self, now: float | None = None) -> list[dict]:
        """
        Expire every active binding whose authoritative token
        (bindings[binding_id].token.expires_at) is past `now`. The device clock
        is the single source of truth — no agent timestamp is consulted.
        Returns a list of expiry-info dicts (see expire_binding).
        """
        now = now if now is not None else time.time()
        with self._lock:
            expired_ids = [
                b.binding_id
                for binds in self.active_binds.values()
                for b in binds
                if (tok := self._token_of(b.binding_id)) is not None and now > tok.expires_at
            ]
            out = []
            for bid in expired_ids:
                info = self.expire_binding(bid)
                if info:
                    out.append({"binding_id": bid, **info})
            return out

    def teardown_all(self, reason: str = "shutdown") -> list[dict]:
        """
        Tear down EVERY active binding through the shared _remove_active_bind path
        (graceful device departure). Unlike expiry it does NOT grant the waitqueue —
        the device is going away, so a freed slot has nothing to hand it to. Returns
        one info dict {binding_id, agent_id, capability_name} per torn-down binding,
        so the runtime can push a shutdown notice to each affected agent.
        """
        with self._lock:
            out = []
            for cap in list(self.active_binds.keys()):
                for bind in list(self.active_binds.get(cap, [])):
                    out.append({"binding_id": bind.binding_id,
                                "agent_id": bind.agent_id,
                                "capability_name": cap})
                    self._remove_active_bind(bind, cap, reason)
            return out

    def cancel_queue(self, agent_id: str, capability_name: str) -> bool:
        """
        Remove agent_id from the waitqueue for capability_name.
        Used by AtomicBinder to prevent ghost bindings when a bind attempt
        returns 'queued' but we want to fail-fast and try a different blueprint.
        Returns True if an entry was removed.
        """
        with self._lock:
            wq = self.waitqueue.get(capability_name, [])
            before = len(wq)
            self.waitqueue[capability_name] = [e for e in wq if e[1] != agent_id]
            return len(wq) > before

    def get_binding(self, binding_id: str) -> Binding | None:
        return self.bindings.get(binding_id)

    def status(self) -> dict:
        with self._lock:
            return {
                "active_binds": {
                    cap: [{"agent_id": b.agent_id, "priority": b.priority} for b in binds]
                    for cap, binds in self.active_binds.items()
                    if binds
                },
                "waitqueue_lengths": {cap: len(q) for cap, q in self.waitqueue.items() if q},
                "quotas": self.quotas,
            }

    def get_history(self) -> list:
        return self.bind_history
