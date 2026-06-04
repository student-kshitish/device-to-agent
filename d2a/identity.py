import hashlib
import hmac
import secrets


def generate_node_id() -> str:
    return secrets.token_hex(8)


def generate_keypair() -> tuple[str, str]:
    private_key = secrets.token_hex(32)
    public_key = hashlib.sha256(private_key.encode()).hexdigest()
    return private_key, public_key


def sign_message(message: str, private_key: str) -> str:
    return hmac.new(private_key.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_signature(message: str, signature: str, public_key: str, private_key: str) -> bool:
    expected = sign_message(message, private_key)
    return hmac.compare_digest(expected, signature)


def sign_capability(capability_name: str, node_id: str, agent_id: str, private_key: str) -> str:
    message = f"{capability_name}:{node_id}:{agent_id}"
    return sign_message(message, private_key)
