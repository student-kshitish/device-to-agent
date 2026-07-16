"""
d2a/arbitration.py — PHASE 12: multi-agent arbitration over a singular resource.

A D2A-ORIGINAL concept (neither MCP nor A2A has it): software tools are copyable,
physical devices are singular. When agent B needs a resource agent A holds, B may
attach a CONTENTION CLAIM to its bind_request stating priority + intent; the
device arbitrates by an OWNER-DECLARED policy; the loser is preempted GRACEFULLY
(notice + re-queue), never silently cut.

THE ANTI-ABUSE CORE: intent is declared, not assumed — and CLAIMING GRANTS
NOTHING. Agents can only state a claim; the owner's ArbitrationPolicy decides
whether that level may preempt. The default is NO preemption for ANY level:
claims order the waitqueue but never evict a holder. (A safety-preempts-by-
default rule was considered and rejected: "safety" is agent-asserted and
unverifiable, so defaulting it to an eviction right hands every remote agent
the same free eviction button this phase exists to close.)

CLAIM SHAPE (optional additive field on bind_request):

    "claim": {"priority": "routine"|"elevated"|"urgent"|"safety",   # fixed set
              "intent":   "<free text, advisory, never parsed>",
              "max_wait": <seconds, optional — how long I'll queue>}

Levels map onto the broker's existing numeric scale (lower = higher priority;
the legacy default is 5), so arbitration EXTENDS the one broker preemption path
instead of forking it: the wire layer computes an effective numeric priority +
a may_preempt flag, and the broker mechanism stays untouched for local callers.

HONEST LIMIT (see README): the device cannot verify a claim is TRUE. A "safety"
claim is an assertion. What IS guaranteed: the claim is attributable (the bind
is Ed25519-signed), bounded (per-agent claim-rate limit), and recorded (signed
audit on every preemption) — a lying claimant is identifiable, not undetectable.

LEAF MODULE: pure stdlib, no d2a imports (pattern of conditions.py/boundary.py).
"""

import threading
import time

# Fixed claim vocabulary → broker numeric priority (lower = higher priority).
# "routine" == 5 == the legacy wire default, so an absent claim and a routine
# claim occupy the same band and neither can preempt the other.
CLAIM_LEVELS = {
    "safety":   1,
    "urgent":   2,
    "elevated": 3,
    "routine":  5,
}
ROUTINE_PRIORITY = CLAIM_LEVELS["routine"]

MAX_INTENT_CHARS = 200

# Anti-gaming default: above-routine claims per agent per sliding window.
DEFAULT_CLAIM_RATE  = 5
DEFAULT_CLAIM_WINDOW = 60.0


class ClaimError(ValueError):
    """Raised when a contention claim violates the fixed vocabulary. The message
    names the exact problem so an agent author can fix the claim."""


def validate_claim(claim) -> dict:
    """
    Validate a wire claim against the fixed vocabulary. Returns the normalized
    claim {priority, intent, max_wait?} on success; raises ClaimError otherwise.
    Intent is advisory text (length-capped, logged, NEVER parsed for decisions);
    priority is the enforced field, weighed by the owner's ArbitrationPolicy.
    """
    if not isinstance(claim, dict):
        raise ClaimError("claim must be an object")
    unknown = set(claim) - {"priority", "intent", "max_wait"}
    if unknown:
        raise ClaimError(f"unknown claim keys {sorted(unknown)}")

    level = claim.get("priority")
    if level not in CLAIM_LEVELS:
        raise ClaimError(f"claim priority must be one of {sorted(CLAIM_LEVELS)}, "
                         f"got {level!r}")

    intent = claim.get("intent", "")
    if not isinstance(intent, str):
        raise ClaimError("claim 'intent' must be a string")
    if len(intent) > MAX_INTENT_CHARS:
        raise ClaimError(f"claim 'intent' exceeds {MAX_INTENT_CHARS} chars")

    out = {"priority": level, "intent": intent}
    if "max_wait" in claim:
        mw = claim["max_wait"]
        if isinstance(mw, bool) or not isinstance(mw, (int, float)) or mw <= 0:
            raise ClaimError("claim 'max_wait' must be a positive number of seconds")
        out["max_wait"] = float(mw)
    return out


class ArbitrationPolicy:
    """
    OWNER-governed arbitration rule. Agents have no verb that can touch this —
    they only declare claims; the owner (or the conservative default) decides.

    Default (nothing configured): NO claim level may preempt — claims order the
    waitqueue by level, holders are never evicted. The owner opts specific
    levels in, globally or per capability:

        device.arbitration.allow_preemption("safety")               # global
        device.arbitration.allow_preemption("urgent", "camera")     # one cap
        device.arbitration.set_claim_rate(max_claims=5, window=60)  # anti-gaming
    """

    def __init__(self) -> None:
        self._global_levels: set[str] = set()          # levels that may preempt anywhere
        self._per_cap: dict[str, set[str]] = {}        # capability -> extra levels
        self._max_claims = DEFAULT_CLAIM_RATE
        self._window     = DEFAULT_CLAIM_WINDOW
        self._claim_times: dict[str, list[float]] = {} # agent_id -> above-routine claim ts
        self._lock = threading.Lock()

    # ── owner API ─────────────────────────────────────────────────────────────

    def allow_preemption(self, level: str, capability: str | None = None) -> None:
        """Owner grants `level` the right to preempt (globally, or for one
        capability). Unknown levels are rejected loudly — a typo must not
        silently grant nothing."""
        if level not in CLAIM_LEVELS:
            raise ClaimError(f"unknown claim level {level!r}; "
                             f"known: {sorted(CLAIM_LEVELS)}")
        with self._lock:
            if capability is None:
                self._global_levels.add(level)
            else:
                self._per_cap.setdefault(capability, set()).add(level)

    def revoke_preemption(self, level: str, capability: str | None = None) -> None:
        """Owner withdraws a previously granted preemption right."""
        with self._lock:
            if capability is None:
                self._global_levels.discard(level)
            else:
                self._per_cap.get(capability, set()).discard(level)

    def set_claim_rate(self, max_claims: int, window: float) -> None:
        """Anti-gaming knob: at most `max_claims` above-routine claims per agent
        per `window` seconds. Exceeding it REFUSES the bind (distinct code,
        audited) rather than silently downgrading — a silent downgrade would lie
        to the claimant."""
        with self._lock:
            self._max_claims = max(1, int(max_claims))
            self._window     = float(window)

    # ── device-side evaluation ────────────────────────────────────────────────

    def may_preempt(self, level: str, capability: str) -> bool:
        """True iff the owner's policy lets a claim at `level` evict a holder of
        `capability`. Absent any grant → False (deny-by-default)."""
        with self._lock:
            return (level in self._global_levels
                    or level in self._per_cap.get(capability, set()))

    def preempt_levels(self, capability: str) -> list[str]:
        """The levels currently allowed to preempt `capability` — recorded in
        the arbitration audit entry so a decision names the policy it ran under."""
        with self._lock:
            return sorted(self._global_levels | self._per_cap.get(capability, set()))

    def note_claim(self, agent_id: str, level: str, now: float | None = None) -> bool:
        """
        Record one above-routine claim by `agent_id` against the sliding-window
        rate limit. Returns True if the claim is within budget, False if it must
        be refused (claim_rate_limited). Routine claims are never limited —
        stating the default costs nothing.
        """
        if level == "routine":
            return True
        now = now if now is not None else time.time()
        with self._lock:
            times = [t for t in self._claim_times.get(agent_id, [])
                     if now - t < self._window]
            if len(times) >= self._max_claims:
                self._claim_times[agent_id] = times
                return False
            times.append(now)
            self._claim_times[agent_id] = times
            return True

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "preempt_levels_global": sorted(self._global_levels),
                "preempt_levels_per_capability": {c: sorted(ls)
                                                  for c, ls in self._per_cap.items() if ls},
                "claim_rate": {"max_claims": self._max_claims, "window": self._window},
            }


def effective_priority(claim: dict | None, legacy_priority) -> int:
    """
    The ONE mapping from a wire request to the broker's numeric priority, for
    REMOTE binds. A validated claim maps to its level's number. No claim → the
    legacy int CLAMPED to the routine band (max(int, 5)): a remote agent keeps
    the right to declare itself LOWER priority, but can no longer self-elevate
    with a raw int — closing the pre-v1.12 hole where any remote bind_request
    with priority:1 silently evicted any holder. Local (in-process) callers
    bypass this entirely (trusted by definition, like every policy gate).
    """
    if claim is not None:
        return CLAIM_LEVELS[claim["priority"]]
    try:
        p = int(legacy_priority)
    except (TypeError, ValueError):
        p = ROUTINE_PRIORITY
    return max(p, ROUTINE_PRIORITY)
