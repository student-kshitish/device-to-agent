"""
d2a/errors.py — the ONE wire-error code registry for the D2A protocol (v1.4).

A pure-constant leaf module: it imports only the two other leaves that already
own trust/identity codes (d2a.signing, d2a.crypto) so every code has exactly one
canonical name and one canonical string value. Nothing in the wire surface may
emit an error/denial code that is not a constant defined or re-exported here.

Unified shapes (v1.4 — the sanctioned pre-adoption break; see README changelog):

  Error (a fault, no useful body):
      {"type": "error", "code": <CODE>, "detail": <human string>,
       ...contextual fields (binding_id, task_id, peer_version) where they exist}

  Coded denial (a semantic response that says "no"): keeps its own type and
  status, but carries the SAME code field from THIS registry:
      {"type": "bind_response"|"lease_renewed"|"released",
       "status": "denied", "code": <CODE>, "detail": <human string>, ...}

  Notice (data-path push, e.g. a dying lease): carries code too, so the agent's
  LeaseLostError.code is uniform with everything else:
      {"type": "lease_expired"|..., "code": <CODE>, ...}

BOUNDARY (Tier F, deferred): codes that appear INSIDE action_result.result —
the Guardian/emergent brain results — are application-level, NOT members of this
registry. They ride nested in a *successful* action_result and are not protocol
control-flow. See README "Error model" for the explicit boundary.
"""

# Imported under private names so d2a.crypto / d2a.signing remain the single
# source of the trust codes, while the ONLY public string constants in this
# module are the canonical UPPER_CASE names below (keeps the registry one-name-
# per-value — see tests/test_errors.py).
from d2a import crypto as _crypto
from d2a import signing as _signing

# ── transport / protocol version ────────────────────────────────────────────
VERSION_MISMATCH = "version_mismatch"

# ── trust / identity (re-exported — signing.py & crypto.py stay the source) ──
UNSIGNED_TRUST_OP           = _signing.ERR_UNSIGNED   # "unsigned_trust_op"
STALE_SIGNATURE             = _signing.ERR_STALE      # "stale_signature"
BAD_SIGNATURE               = _signing.ERR_BAD_SIG    # "bad_signature"
NODE_ID_DERIVATION_MISMATCH = _crypto.ERR_DERIVATION  # "node_id_derivation_mismatch"
TOFU_KEY_MISMATCH           = _crypto.ERR_PIN         # "tofu_key_mismatch"

# ── lease / binding lifecycle (renew + release denials, lease-death notices) ─
UNKNOWN_BINDING     = "unknown_binding"
NOT_OWNER           = "not_owner"
CAPABILITY_MISMATCH = "capability_mismatch"
LEASE_EXPIRED       = "lease_expired"    # renew denial AND the death push (was "expired"/"ttl_expired")
DEVICE_SHUTDOWN     = "device_shutdown"  # graceful departure (Part 2) — sibling of LEASE_EXPIRED

# ── policy (were human-message-only before v1.4) ─────────────────────────────
POLICY_BLOCKED    = "policy_blocked"
APPROVAL_REQUIRED = "approval_required"

# ── broker (were {"status":"error","message":...} before v1.4) ───────────────
CAPABILITY_NOT_FOUND = "capability_not_found"
NO_ACTIVE_BIND       = "no_active_bind"
BINDING_NOT_FOUND    = "binding_not_found"

# ── binding-scope / action / event guards (device data-path handlers) ────────
BINDING_INVALID_OR_OUT_OF_SCOPE = "binding_invalid_or_out_of_scope"
NOT_AN_ACTION_CAPABILITY        = "not_an_action_capability"
NO_MANIFEST_FOR_CONDITIONS      = "no_manifest_for_conditions"
INVALID_CONDITION               = "invalid_condition"
EVENT_CAP_EXCEEDED              = "event_cap_exceeded"
DEVICE_EVENT_CAPACITY           = "device_event_capacity"

# ── agent-side (never leave the agent, but share the one shape) ──────────────
NO_RESPONSE         = "no_response"
BINDING_ID_MISMATCH = "binding_id_mismatch"
NO_PROVIDER         = "no_provider"

# The full set — the enforcement test asserts no duplicate values and that no
# wire module emits a code string absent from here.
ALL_CODES = frozenset({
    VERSION_MISMATCH,
    UNSIGNED_TRUST_OP, STALE_SIGNATURE, BAD_SIGNATURE,
    NODE_ID_DERIVATION_MISMATCH, TOFU_KEY_MISMATCH,
    UNKNOWN_BINDING, NOT_OWNER, CAPABILITY_MISMATCH, LEASE_EXPIRED, DEVICE_SHUTDOWN,
    POLICY_BLOCKED, APPROVAL_REQUIRED,
    CAPABILITY_NOT_FOUND, NO_ACTIVE_BIND, BINDING_NOT_FOUND,
    BINDING_INVALID_OR_OUT_OF_SCOPE, NOT_AN_ACTION_CAPABILITY,
    NO_MANIFEST_FOR_CONDITIONS, INVALID_CONDITION,
    EVENT_CAP_EXCEEDED, DEVICE_EVENT_CAPACITY,
    NO_RESPONSE, BINDING_ID_MISMATCH, NO_PROVIDER,
})


def is_code(value: str) -> bool:
    """True if `value` is a registered wire-error code."""
    return value in ALL_CODES


def error(code: str, detail: str = "", **context) -> dict:
    """Build a unified `type:"error"` message. Contextual fields (binding_id,
    task_id, peer_version, …) go in via **context."""
    msg = {"type": "error", "code": code}
    if detail:
        msg["detail"] = detail
    msg.update(context)
    return msg


class WireError(Exception):
    """
    Raised agent-side when a wire response is the unified error shape. Carries
    the registry `.code` (and `.detail`) so callers branch on a stable code, not
    on parsing a human string.
    """
    def __init__(self, code: str, detail: str = "", binding_id: str = None):
        self.code = code
        self.detail = detail
        self.binding_id = binding_id
        super().__init__(f"{code}" + (f": {detail}" if detail else ""))

    @classmethod
    def from_response(cls, response: dict) -> "WireError":
        return cls(response.get("code", ""), response.get("detail", ""),
                   response.get("binding_id"))
