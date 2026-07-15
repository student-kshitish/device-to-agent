from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    name: str
    tags: list[str]
    live_state: dict
    node_id: str
    public_key: str = ""
    # v1.2 (additive): optional machine-readable self-description. When set, it
    # is copied into the published record (before signing) so it is authenticated
    # for free. None = no manifest shipped (records stay valid — additive).
    manifest: dict | None = None


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
    # v1.1 (additive): ts anchors replay/issue time; sig_key is the issuing
    # device's Ed25519 public key. The signature now covers ALL of the above
    # (except itself) — closing the old HMAC gap where expires_at/scope were
    # unsigned. Defaults keep any positional/legacy construction from breaking.
    ts: float = 0.0
    sig_key: str = ""


@dataclass(frozen=True)
class KeyPair:
    node_id: str
    private_key: str
    public_key: str

    def __repr__(self) -> str:
        # Never expose the private seed in logs/reprs/tracebacks.
        return (f"KeyPair(node_id={self.node_id!r}, "
                f"public_key={self.public_key!r}, private_key=<redacted>)")


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
    status: str = "active"  # active | rebound | released | expired | preempted | revoked
    # Why the binding left the active set. One of: "" (still active) | released |
    # preempted | expired | revoked. Set only through the broker's shared teardown path.
    release_reason: str = ""
    # ── lease delegation (Phase 10B; additive) ────────────────────────────────
    # A CHILD binding: issued when agent A delegates this capability to agent B.
    # parent_binding_id links it to A's binding (the umbrella lease); delegated_by
    # is A's agent_id; scope_restrict optionally narrows what B may do (a subset of
    # the capability's actions, never wider). All three default empty/None so a
    # normal (non-delegated) binding is unchanged. A child is device-issued, capped
    # to the parent's expiry, non-renewable, and torn down when the parent ends.
    parent_binding_id: str = ""
    delegated_by: str = ""
    scope_restrict: dict | None = None
