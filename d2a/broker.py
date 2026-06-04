import time
from dataclasses import dataclass

from d2a.schema import BindToken, Binding
from d2a.verbs import make_bind_request, make_bind_token, make_binding


@dataclass
class ActiveBind:
    token: BindToken
    agent_id: str
    capability_name: str
    priority: int
    bound_at: float
    needs: list[str]
    binding_id: str = ""


class CapabilityBroker:
    def __init__(self, runtime):
        self.runtime = runtime
        self.active_binds: dict[str, list[ActiveBind]] = {}
        self.quotas: dict[str, int] = {}
        # waitqueue entries: (priority, agent_id, needs)
        self.waitqueue: dict[str, list] = {}
        self.bindings: dict[str, Binding] = {}
        self.bind_history: list = []

    def _log(self, event: str, **kwargs):
        self.bind_history.append({"time": time.time(), "event": event, **kwargs})

    def _issue_token(self, agent_id: str, capability_name: str, needs: list[str], priority: int) -> ActiveBind:
        req = make_bind_request(agent_id, capability_name, needs, priority)
        token = make_bind_token(req, self.runtime.node_id, self.runtime.private_key)
        binding = make_binding(token)
        self.bindings[binding.binding_id] = binding
        return ActiveBind(
            token=token,
            agent_id=agent_id,
            capability_name=capability_name,
            priority=priority,
            bound_at=time.time(),
            needs=needs,
            binding_id=binding.binding_id,
        )

    def request_bind(self, agent_id: str, capability_name: str, needs: list[str], priority: int = 5) -> dict:
        if self.runtime.get_capability(capability_name) is None:
            return {"status": "error", "message": f"Capability '{capability_name}' not found"}

        quota = self.quotas.get(capability_name, 1)
        active = self.active_binds.setdefault(capability_name, [])
        wq = self.waitqueue.setdefault(capability_name, [])

        if len(active) < quota:
            bind = self._issue_token(agent_id, capability_name, needs, priority)
            active.append(bind)
            self._log("granted", agent_id=agent_id, capability=capability_name, priority=priority)
            return {
                "status": "granted",
                "token": bind.token,
                "binding_id": bind.binding_id,
                "message": f"Bound to {capability_name}",
            }

        # slot full — check for preemption candidate
        worst = max(active, key=lambda b: b.priority)
        if worst.priority > priority:
            active.remove(worst)
            wq.append((worst.priority, worst.agent_id, worst.needs))
            wq.sort(key=lambda x: x[0])
            self._log("preempted", agent_id=worst.agent_id, capability=capability_name, by=agent_id)

            bind = self._issue_token(agent_id, capability_name, needs, priority)
            active.append(bind)
            self._log("granted_by_preemption", agent_id=agent_id, capability=capability_name, priority=priority)
            return {
                "status": "granted_by_preemption",
                "token": bind.token,
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
        active = self.active_binds.get(capability_name, [])
        bind = next((b for b in active if b.agent_id == agent_id), None)
        if bind is None:
            return {"status": "error", "message": f"No active bind for agent {agent_id} on {capability_name}"}

        active.remove(bind)
        if bind.binding_id in self.bindings:
            self.bindings[bind.binding_id].status = "released"
        self._log("released", agent_id=agent_id, capability=capability_name)

        wq = self.waitqueue.get(capability_name, [])
        if wq:
            next_priority, next_agent_id, next_needs = wq.pop(0)
            new_bind = self._issue_token(next_agent_id, capability_name, next_needs, next_priority)
            active.append(new_bind)
            self._log("auto_granted", agent_id=next_agent_id, capability=capability_name)
            return {
                "status": "released",
                "next_agent_id": next_agent_id,
                "token": new_bind.token,
                "binding_id": new_bind.binding_id,
            }

        return {"status": "released", "next_agent_id": None}

    def cancel_queue(self, agent_id: str, capability_name: str) -> bool:
        """
        Remove agent_id from the waitqueue for capability_name.
        Used by AtomicBinder to prevent ghost bindings when a bind attempt
        returns 'queued' but we want to fail-fast and try a different blueprint.
        Returns True if an entry was removed.
        """
        wq = self.waitqueue.get(capability_name, [])
        before = len(wq)
        self.waitqueue[capability_name] = [e for e in wq if e[1] != agent_id]
        return len(wq) > before

    def get_binding(self, binding_id: str) -> Binding | None:
        return self.bindings.get(binding_id)

    def status(self) -> dict:
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
