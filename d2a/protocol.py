"""
d2a/protocol.py — wire protocol version + negotiation helpers.

LEAF MODULE: imports only stdlib so every transport module (swarm.py,
kademlia.py) and both endpoints can import it without an import cycle
(d2a/__init__ imports swarm, so swarm must not import the d2a package).
PROTOCOL_VERSION and ProtocolVersionError are re-exported from d2a/__init__.

Wire versioning contract (as of v1.0 — the lease-work freeze point):
  - Every outbound message and published record carries a top-level "v"
    field, injected at the serialization chokepoints (NOT an envelope, so
    existing handlers that read msg["type"]/fields are untouched).
  - Same MAJOR  → compatible. Process normally; unknown fields are IGNORED
    (minor versions are additive-only).
  - Different MAJOR → incompatible. TCP requests get a version_mismatch
    error; UDP messages are dropped without reply (no error-reply loops).
  - Missing "v" (legacy 0.x) → accepted for now with a one-time deprecation
    warning. Planned to be REJECTED in the next major.

Version format is "MAJOR.MINOR" as a string (e.g. "1.0"): human-readable in
wire dumps/logs and unambiguous in JSON. Major is int(v.split(".")[0]).
"""

import logging

PROTOCOL_VERSION = "1.2"
VERSION_FIELD = "v"

logger = logging.getLogger("d2a.protocol")

_warned_legacy: set = set()


class ProtocolVersionError(Exception):
    """
    Raised on the agent side when a peer reports an incompatible (different
    major) protocol version. Names both versions so the failure is actionable.
    """
    def __init__(self, local_version: str, peer_version: str | None):
        self.local_version = local_version
        self.peer_version = peer_version
        super().__init__(
            f"protocol version mismatch: local={local_version} peer={peer_version}"
        )


def major_of(version: str | None) -> int:
    """Major component of a version string. None/"" → 0 (legacy). Unparseable → -1."""
    if not version:
        return 0
    try:
        return int(str(version).split(".", 1)[0])
    except (ValueError, TypeError):
        return -1


def versions_compatible(a: str | None, b: str | None) -> bool:
    """Two versions are compatible iff they share a major (and both parse)."""
    ma, mb = major_of(a), major_of(b)
    return ma == mb and ma >= 0


def stamp(msg: dict, version: str = PROTOCOL_VERSION) -> dict:
    """
    Inject our version into an outbound message/record (top-level, in place).
    Overwrites any existing top-level "v" — the stamper is the immediate sender.
    Relayed record payloads are NOT re-stamped (kademlia never calls stamp on a
    nested record), so a record keeps its author's version as it propagates.
    """
    if isinstance(msg, dict):
        msg[VERSION_FIELD] = version
    return msg


def classify(peer_version: str | None) -> str:
    """
    Classify an INBOUND peer version against ours:
      "current"      — same major, process normally (ignore unknown fields)
      "legacy"       — no version field, treat as 0.x (accept + warn for now)
      "incompatible" — different/unparseable major (reject/drop)
    """
    if peer_version is None:
        return "legacy"
    return "current" if versions_compatible(peer_version, PROTOCOL_VERSION) else "incompatible"


def warn_legacy_once(tag: str = "") -> None:
    """One-time deprecation warning per source tag, to avoid log spam."""
    if tag in _warned_legacy:
        return
    _warned_legacy.add(tag)
    logger.warning(
        "legacy versionless message from %s accepted (treated as 0.x); this will "
        "be REJECTED in the next protocol major. Upgrade the peer to v%s.",
        tag or "unknown", PROTOCOL_VERSION,
    )
