"""
d2a/identity.py — node identity + capability-token signing (Ed25519).

The HMAC placeholder is GONE. All asymmetric primitives live in d2a.crypto;
this module wires them to D2A's identity concepts.

  - generate_node_id(): a random 16-hex handle. Used ONLY for binding_id now —
    a capability handle is deliberately unpredictable and is NOT an identity.
    Node/agent IDENTITY is derived from a public key (crypto.derive_node_id),
    so a peer cannot claim an arbitrary id with a key it controls.

  - BindToken signing: a token is signed by the ISSUING DEVICE over a canonical
    payload covering capability_name, agent_id, node_id, scope, expires_at, ts
    and the signer key (sig_key). Every one of those fields is therefore
    tamper-evident — this closes the old HMAC coverage gap, where only
    "cap:node:agent" was signed and expires_at/scope rode along unauthenticated.
    Verification uses the device's PUBLIC key (no private key required), so any
    peer holding the pinned device key can validate a token offline.
"""

import secrets

from d2a import crypto

# Re-exports so existing import sites keep working after the HMAC removal.
generate_keypair = crypto.generate_keypair
derive_node_id = crypto.derive_node_id


def generate_node_id() -> str:
    """
    Random 16-hex handle. USED ONLY FOR binding_id (a capability handle).
    Node/agent identity is pubkey-derived — see crypto.derive_node_id.
    """
    return secrets.token_hex(8)


def _token_payload(capability_name, agent_id, node_id, scope, expires_at, ts, sig_key) -> dict:
    """The exact field set covered by a BindToken signature (sig_key included)."""
    return {
        "capability_name": capability_name,
        "agent_id":        agent_id,
        "node_id":         node_id,
        "scope":           scope,
        "expires_at":      expires_at,
        "ts":              ts,
        "sig_key":         sig_key,
    }


def sign_bind_token(capability_name, agent_id, node_id, scope, expires_at, ts,
                    private_key: str, public_key: str) -> str:
    """Device-sign a token's canonical payload. Returns the signature hex."""
    payload = _token_payload(capability_name, agent_id, node_id, scope, expires_at, ts, public_key)
    return crypto.sign(crypto.canonical_json(payload), private_key).hex()


def verify_bind_token_sig(token, device_pubkey: str) -> bool:
    """
    Verify a token's signature against the device's public key. The token's own
    sig_key must equal device_pubkey (the caller is responsible for having
    TOFU-pinned / derivation-checked device_pubkey against the node_id).
    """
    if getattr(token, "sig_key", "") != device_pubkey:
        return False
    payload = _token_payload(
        token.capability_name, token.agent_id, token.node_id,
        token.scope, token.expires_at, token.ts, token.sig_key,
    )
    try:
        sig = bytes.fromhex(token.signature)
    except (ValueError, TypeError):
        return False
    return crypto.verify(crypto.canonical_json(payload), sig, device_pubkey)
