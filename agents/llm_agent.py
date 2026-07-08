from d2a import crypto


class LLMAgent:
    """
    Minimal in-process agent. Identity is a real Ed25519 keypair (ephemeral —
    a long-lived agent would use RemoteAgent's persisted identity); agent_id is
    derived from the public key.

    In the Ed25519 model an agent NEVER holds the device's signing key. To get a
    binding it asks the DEVICE to mint one (the device signs the token with its
    own key) and then verifies the returned token against the device's PUBLIC
    key — proving the token came from the real device, not a forgery.
    """

    def __init__(self):
        self.private_key, self.public_key = crypto.generate_keypair()
        self.agent_id = crypto.derive_node_id(self.public_key)
        self.needs = ["gpu"]

    def request_bind(self, runtime, capability_name: str = "gpu"):
        cap = runtime.get_capability(capability_name)
        if cap is None:
            raise ValueError(f"Capability '{capability_name}' not found on runtime")
        result = runtime.broker_request(self.agent_id, capability_name, self.needs)
        if result.get("status") not in ("granted", "granted_by_preemption"):
            raise ValueError(f"bind not granted: {result.get('status')} — {result.get('message', '')}")
        token = result["token"]
        if not runtime.verify_agent_token(token):
            raise ValueError("Token signature invalid — possible fake runtime")
        return token
