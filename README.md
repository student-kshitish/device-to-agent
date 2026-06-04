# D2A Protocol

Device-to-Agent binding layer. Frozen contract. Runtimes and agents evolve independently.

## The three frozen pieces

- **Capability schema** (`d2a/schema.py`) — `Capability`, `BindRequest`, `BindToken` as frozen dataclasses. This is the contract that never changes.
- **Bind/rebind verbs** (`d2a/verbs.py`) — `make_bind_request`, `make_bind_token`, `verify_token`. The protocol actions over the schema.
- **Identity + token format** (`d2a/identity.py`) — `generate_node_id` (16-char hex), `sign_capability` (sha256 of capability:node:agent). No external deps.

## Run

```
python examples/bind_one.py
```

## Built on top of

[EdgeMind swarm](https://github.com/student-kshitish/anp-edge-swarm)
