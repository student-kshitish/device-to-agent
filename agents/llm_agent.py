from d2a import generate_node_id, make_bind_request, make_bind_token, BindToken


class LLMAgent:
    def __init__(self):
        self.agent_id = generate_node_id()
        self.needs = ["gpu"]

    def request_bind(self, runtime, capability_name: str = "gpu") -> BindToken:
        cap = runtime.get_capability(capability_name)
        if cap is None:
            raise ValueError(f"Capability '{capability_name}' not found on runtime")
        req = make_bind_request(self.agent_id, capability_name, self.needs)
        token = make_bind_token(req, runtime.node_id, runtime.private_key)
        if not runtime.verify_agent_token(token):
            raise ValueError("Token signature invalid — possible fake runtime")
        return token
