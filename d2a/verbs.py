import time

from d2a.schema import BindRequest, BindToken, Binding
from d2a.identity import sign_bind_token, verify_bind_token_sig, generate_node_id


def make_bind_request(agent_id: str, capability_name: str, needs: list[str], priority: int = 5) -> BindRequest:
    return BindRequest(agent_id=agent_id, capability_name=capability_name, needs=needs, priority=priority)


def make_bind_token(req: BindRequest, node_id: str, private_key: str, public_key: str,
                    ttl_seconds: int = 300) -> BindToken:
    """Issue a device-signed Ed25519 token. The device's private_key signs, its
    public_key is embedded as sig_key so any pinned peer can verify offline."""
    now = time.time()
    expires_at = now + ttl_seconds
    signature = sign_bind_token(
        req.capability_name, req.agent_id, node_id, req.capability_name,
        expires_at, now, private_key, public_key,
    )
    return BindToken(
        capability_name=req.capability_name,
        agent_id=req.agent_id,
        node_id=node_id,
        scope=req.capability_name,
        expires_at=expires_at,
        signature=signature,
        ts=now,
        sig_key=public_key,
    )


def verify_token(token: BindToken) -> bool:
    return time.time() < token.expires_at


def verify_bind_token(token: BindToken, device_pubkey: str) -> bool:
    """Verify a token against the ISSUING DEVICE'S PUBLIC key (no private key).
    Checks liveness (not expired) AND the Ed25519 signature over all fields."""
    if not verify_token(token):
        return False
    return verify_bind_token_sig(token, device_pubkey)


def make_binding(token: BindToken) -> Binding:
    return Binding(
        binding_id=generate_node_id(),
        token=token,
        agent_id=token.agent_id,
        node_id=token.node_id,
        capability_name=token.capability_name,
        scope=token.scope,
        created_at=time.time(),
    )


def rebind(binding: Binding, new_capability_name: str, runtime, private_key: str) -> Binding:
    req = make_bind_request(binding.agent_id, new_capability_name, [])
    binding.token = make_bind_token(req, runtime.node_id, private_key, runtime.public_key)
    binding.capability_name = new_capability_name
    binding.scope = new_capability_name
    binding.rebind_count += 1
    binding.status = "active"
    return binding


def renew(binding: Binding, runtime, private_key: str, ttl_seconds: int = 300) -> Binding:
    req = make_bind_request(binding.agent_id, binding.capability_name, [])
    binding.token = make_bind_token(req, runtime.node_id, private_key, runtime.public_key, ttl_seconds)
    binding.status = "active"
    return binding


def unbind(binding: Binding) -> Binding:
    binding.status = "released"
    return binding
