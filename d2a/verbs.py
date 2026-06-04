import hmac
import time

from d2a.schema import BindRequest, BindToken, Binding
from d2a.identity import sign_capability, generate_node_id


def make_bind_request(agent_id: str, capability_name: str, needs: list[str], priority: int = 5) -> BindRequest:
    return BindRequest(agent_id=agent_id, capability_name=capability_name, needs=needs, priority=priority)


def make_bind_token(req: BindRequest, node_id: str, private_key: str, ttl_seconds: int = 300) -> BindToken:
    return BindToken(
        capability_name=req.capability_name,
        agent_id=req.agent_id,
        node_id=node_id,
        scope=req.capability_name,
        expires_at=time.time() + ttl_seconds,
        signature=sign_capability(req.capability_name, node_id, req.agent_id, private_key),
    )


def verify_token(token: BindToken) -> bool:
    return time.time() < token.expires_at


def verify_bind_token(token: BindToken, private_key: str) -> bool:
    if not verify_token(token):
        return False
    expected = sign_capability(token.capability_name, token.node_id, token.agent_id, private_key)
    return hmac.compare_digest(expected, token.signature)


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
    binding.token = make_bind_token(req, runtime.node_id, private_key)
    binding.capability_name = new_capability_name
    binding.scope = new_capability_name
    binding.rebind_count += 1
    binding.status = "active"
    return binding


def renew(binding: Binding, runtime, private_key: str, ttl_seconds: int = 300) -> Binding:
    req = make_bind_request(binding.agent_id, binding.capability_name, [])
    binding.token = make_bind_token(req, runtime.node_id, private_key, ttl_seconds)
    binding.status = "active"
    return binding


def unbind(binding: Binding) -> Binding:
    binding.status = "released"
    return binding
