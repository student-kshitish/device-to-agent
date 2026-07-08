from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    name: str
    tags: list[str]
    live_state: dict
    node_id: str
    public_key: str = ""


@dataclass(frozen=True)
class BindRequest:
    agent_id: str
    capability_name: str
    needs: list[str]
    priority: int = 5


@dataclass(frozen=True)
class BindToken:
    capability_name: str
    agent_id: str
    node_id: str
    scope: str
    expires_at: float
    signature: str


@dataclass(frozen=True)
class KeyPair:
    node_id: str
    private_key: str
    public_key: str


@dataclass
class Binding:
    binding_id: str
    token: BindToken
    agent_id: str
    node_id: str
    capability_name: str
    scope: str
    created_at: float
    rebind_count: int = 0
    status: str = "active"  # active | rebound | released | expired | preempted
    # Why the binding left the active set. One of: "" (still active) | released |
    # preempted | expired. Set only through the broker's shared teardown path.
    release_reason: str = ""
