"""
d2a/signing.py — wire-level signing + verification for the trust messages.

Sits above d2a.crypto (which does the raw Ed25519 + canonical JSON + TOFU pin
logic) and below the transport. Handles the protocol-specific concerns:

  - v + ts go INSIDE the signed payload (ruling #4). We stamp them here, BEFORE
    signing. The transport's stamp() then re-sets v to the same value — an
    idempotent no-op — so the signature stays valid on the wire.
  - Replay: a signed message with |receiver_now - ts| > REPLAY_WINDOW is stale
    (device clock authoritative, consistent with the lease design).
  - VERIFICATION DISCIPLINE (hard rule): there is no bare verify_dict here.
    verify_message always runs BOTH the derivation check (node_id must derive
    from sig_key) AND the TOFU pin check — via PinStore.verify — and only then
    the signature check, each with a distinct error reason. A self-consistent
    forgery that claims another node's id fails the derivation check.

Signed message types (v1.1): the five trust operations
    bind_request, renew_binding, release_binding   (agent → device)
    bind_response, lease_renewed                     (device → agent)
and published capability records (device-signed). get_reading / subscribe /
stream_frame stay binding_id-bearer and are NOT signed (ruling #2).
"""

import time

from d2a import crypto
from d2a.protocol import PROTOCOL_VERSION

REPLAY_WINDOW_SECONDS = 60

# Distinct rejection reasons (kept apart from protocol.py's version_mismatch).
ERR_UNSIGNED = "unsigned_trust_op"    # a trust message arrived with no signature
ERR_STALE = "stale_signature"          # ts outside the replay window
ERR_BAD_SIG = "bad_signature"          # signature did not verify
# crypto.ERR_DERIVATION / crypto.ERR_PIN are reused verbatim for identity faults.

# The agent→device trust operations that MUST be signed or be hard-rejected.
SIGNED_REQUEST_TYPES = frozenset({"bind_request", "renew_binding", "release_binding"})


def sign_message(msg: dict, private_key: str, public_key: str, ts: float | None = None) -> dict:
    """
    Return a signed copy of msg: stamp v + ts into the payload, then Ed25519-sign
    it (sig_key inside the signed bytes, sig outside). Idempotent w.r.t. the
    transport version stamp because v is set to PROTOCOL_VERSION here already.
    """
    out = dict(msg)
    out["v"] = PROTOCOL_VERSION
    out["ts"] = time.time() if ts is None else ts
    return crypto.sign_dict(out, private_key, public_key)


def sign_record(record: dict, private_key: str, public_key: str) -> dict:
    """
    Device-sign a capability record. The transport REWRITES a record's `ts` on
    every ingest (for TTL bookkeeping), so `ts` is deliberately EXCLUDED from the
    signed bytes — a record's freshness is bounded by the discovery TTL, not a
    signed replay window (ruling #5: no separate replay check for records). The
    stable identity/capability fields (+ v + sig_key) are what's signed. sig_key
    equals the record's public_key.
    """
    out = dict(record)
    out["v"] = PROTOCOL_VERSION
    ts = out.pop("ts", None)                       # transport-managed, not signed
    signed = crypto.sign_dict(out, private_key, public_key)
    if ts is not None:
        signed["ts"] = ts                          # reattach, unsigned
    return signed


def is_signed(msg: dict) -> bool:
    return isinstance(msg, dict) and "sig" in msg and "sig_key" in msg


def verify_record(record: dict, pins: "crypto.PinStore") -> str | None:
    """
    Verify a signed capability record and pin the device key. No replay window —
    record freshness is bounded by the transport's discovery TTL (ruling #5).
    `ts` is excluded from the verified bytes (the transport rewrites it). The
    advertised public_key must equal the signer key.
    """
    if not is_signed(record):
        return ERR_UNSIGNED
    sig_key = record["sig_key"]
    pub = record.get("public_key")
    if pub and pub != sig_key:
        return ERR_BAD_SIG
    pin_err = pins.verify(record.get("node_id", ""), sig_key)
    if pin_err:
        return pin_err
    payload = {k: v for k, v in record.items() if k not in ("sig", "ts")}
    try:
        sig = bytes.fromhex(record["sig"])
    except (ValueError, TypeError):
        return ERR_BAD_SIG
    if not crypto.verify(crypto.canonical_json(payload), sig, sig_key):
        return ERR_BAD_SIG
    return None


def verify_message(msg: dict, claimed_node_id: str, pins: "crypto.PinStore",
                   now: float | None = None) -> str | None:
    """
    Verify a signed inbound trust message. Returns None if acceptable, else a
    distinct error reason:
      ERR_UNSIGNED            — no sig/sig_key present
      crypto.ERR_DERIVATION   — claimed_node_id does not derive from sig_key
      crypto.ERR_PIN          — known node_id presenting a different key (TOFU)
      ERR_BAD_SIG             — signature failed to verify
      ERR_STALE               — ts outside the replay window

    `claimed_node_id` is the identity the message asserts (from_node for an
    agent→device message; the dialed provider node_id for a device→agent one).
    Pinning binds that identity to the signing key.
    """
    if not is_signed(msg):
        return ERR_UNSIGNED
    sig_key = msg["sig_key"]

    # Discipline: derivation + pin (both, with distinct reasons) BEFORE trusting.
    pin_err = pins.verify(claimed_node_id, sig_key)
    if pin_err:
        return pin_err

    # Signature must verify against the now-trusted key (never a bare verify_dict).
    if not crypto.verify_dict(msg, expected_pubkey_hex=sig_key):
        return ERR_BAD_SIG

    # Replay window — receiver clock is authoritative.
    ts = msg.get("ts")
    if not isinstance(ts, (int, float)):
        return ERR_STALE
    now = time.time() if now is None else now
    if abs(now - ts) > REPLAY_WINDOW_SECONDS:
        return ERR_STALE

    return None
