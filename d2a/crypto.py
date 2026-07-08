"""
d2a/crypto.py — Ed25519 trust primitives for D2A.

Replaces the old HMAC placeholder in identity.py with real asymmetric
signatures. Identity is now a keypair: a node_id is DERIVED from its public
key, so a peer cannot claim an arbitrary node_id with a key it controls.

BACKENDS (auto-detected at import, in preference order):
    1. PyNaCl            (libsodium — fast, constant-time)
    2. cryptography      (OpenSSL   — fast, constant-time)
    3. pure-Python       (d2a._ed25519_fallback — DEMO-GRADE, NOT constant time)

The wire format and the signature BYTES are identical across all three
(Ed25519 / RFC 8032 is deterministic), so nodes on different backends
interoperate perfectly. The pure fallback keeps the core dependency-free; a
production deployment installs the [crypto] extra and one of the real backends
is selected automatically. See ACTIVE_BACKEND.

LEAF MODULE: stdlib + optional backends only. No d2a imports except the pure
fallback and the KeyPair dataclass, so it stays importable without pulling in
the transport stack.

Canonical signing (exactly as designed):
  - canonical_json: sorted keys, compact separators, ensure_ascii=False, UTF-8.
  - sign_dict: sig_key (signer pubkey) goes INSIDE the signed bytes; the
    resulting "sig" hex is added OUTSIDE (it is the output, not an input).
  - verify_dict: recompute over the message minus "sig"; optionally enforce a
    TOFU-pinned expected pubkey.
"""

import hashlib
import json
import os
import stat
from pathlib import Path

from d2a.schema import KeyPair

# ── backend detection (pynacl → cryptography → pure fallback) ────────────────

def _load_backend():
    try:
        from nacl.signing import SigningKey, VerifyKey  # noqa: F401
        from nacl.exceptions import BadSignatureError

        def pub_from_seed(seed: bytes) -> bytes:
            return bytes(SigningKey(seed).verify_key)

        def sign_raw(msg: bytes, seed: bytes) -> bytes:
            return SigningKey(seed).sign(msg).signature

        def verify_raw(msg: bytes, sig: bytes, pub: bytes) -> bool:
            try:
                VerifyKey(pub).verify(msg, sig)
                return True
            except (BadSignatureError, ValueError):
                return False

        return "pynacl", pub_from_seed, sign_raw, verify_raw
    except ImportError:
        pass

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey, Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )
        from cryptography.exceptions import InvalidSignature

        def pub_from_seed(seed: bytes) -> bytes:
            return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            )

        def sign_raw(msg: bytes, seed: bytes) -> bytes:
            return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)

        def verify_raw(msg: bytes, sig: bytes, pub: bytes) -> bool:
            try:
                Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
                return True
            except (InvalidSignature, ValueError):
                return False

        return "cryptography", pub_from_seed, sign_raw, verify_raw
    except ImportError:
        pass

    from d2a import _ed25519_fallback as fb

    def pub_from_seed(seed: bytes) -> bytes:
        return fb.secret_to_public(seed)

    def sign_raw(msg: bytes, seed: bytes) -> bytes:
        return fb.sign(seed, msg)

    def verify_raw(msg: bytes, sig: bytes, pub: bytes) -> bool:
        return fb.verify(pub, msg, sig)

    return "fallback", pub_from_seed, sign_raw, verify_raw


ACTIVE_BACKEND, _pub_from_seed, _sign_raw, _verify_raw = _load_backend()

SEED_BYTES = 32   # Ed25519 private seed length
PUB_BYTES = 32    # Ed25519 public key length
SIG_BYTES = 64    # Ed25519 signature length

# TOFU / identity error reasons — kept distinct so callers (and 2B wire code)
# can report exactly WHY an identity was rejected.
ERR_DERIVATION = "node_id_derivation_mismatch"   # node_id does not derive from pubkey
ERR_PIN = "tofu_key_mismatch"                     # known node_id, different key


def using_fallback() -> bool:
    """True iff the demo-grade pure-Python backend is active (no real backend installed)."""
    return ACTIVE_BACKEND == "fallback"


# ── low-level key ops ────────────────────────────────────────────────────────

def generate_keypair() -> tuple[str, str]:
    """Return (private_seed_hex, public_key_hex). The 'private key' is the 32-byte seed."""
    seed = os.urandom(SEED_BYTES)
    pub = _pub_from_seed(seed)
    return seed.hex(), pub.hex()


def public_from_private(private_hex: str) -> str:
    """Derive the public key hex from a private seed hex."""
    return _pub_from_seed(bytes.fromhex(private_hex)).hex()


def sign(msg: bytes, private_hex: str) -> bytes:
    """Ed25519-sign raw bytes with a private seed hex. Returns the 64-byte signature."""
    seed = bytes.fromhex(private_hex)
    if len(seed) != SEED_BYTES:
        raise ValueError(f"private key must be {SEED_BYTES} bytes ({SEED_BYTES*2} hex chars)")
    return _sign_raw(msg, seed)


def verify(msg: bytes, sig: bytes, public_hex: str) -> bool:
    """Verify a signature over raw bytes against a public key hex. Never raises."""
    try:
        pub = bytes.fromhex(public_hex)
    except (ValueError, TypeError):
        return False
    if len(pub) != PUB_BYTES or len(sig) != SIG_BYTES:
        return False
    return _verify_raw(msg, sig, pub)


def derive_node_id(public_hex: str) -> str:
    """
    Identity binding: node_id = first 16 hex chars of sha256(pubkey bytes).
    16 hex chars == the width of the old secrets.token_hex(8) node ids, so all
    existing node_id[:8] slicing / dict keys / routing are unaffected in shape.
    An attacker cannot pick a node_id independently of the key it controls.
    """
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:16]


def identity_matches(node_id: str, public_hex: str) -> bool:
    """Stateless derivation check: does node_id derive from this public key?"""
    try:
        return derive_node_id(public_hex) == node_id
    except (ValueError, TypeError):
        return False


# ── canonical JSON + dict signing ────────────────────────────────────────────

def canonical_json(obj) -> bytes:
    """Deterministic serialization: sorted keys, compact separators, UTF-8, unicode preserved."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_dict(msg: dict, private_hex: str, public_hex: str) -> dict:
    """
    Return a copy of msg with a detached Ed25519 signature attached.

    sig_key (the signer's public key) is placed INSIDE the signed payload so it
    cannot be swapped for another key without invalidating the signature. The
    resulting "sig" hex is added OUTSIDE the signed bytes (it is the output).
    Any pre-existing "sig"/"sig_key" on the input are ignored/overwritten.
    """
    payload = {k: v for k, v in msg.items() if k not in ("sig", "sig_key")}
    payload["sig_key"] = public_hex
    sig = sign(canonical_json(payload), private_hex)
    return {**payload, "sig": sig.hex()}


def verify_dict(msg: dict, expected_pubkey_hex: str | None = None) -> bool:
    """
    Verify a dict produced by sign_dict. Recomputes over the message minus
    "sig" (sig_key stays in, since it was signed). If expected_pubkey_hex is
    given (a TOFU-pinned key), the message's sig_key must equal it.
    """
    if not isinstance(msg, dict) or "sig" not in msg or "sig_key" not in msg:
        return False
    pk = msg["sig_key"]
    if expected_pubkey_hex is not None and pk != expected_pubkey_hex:
        return False
    try:
        sig = bytes.fromhex(msg["sig"])
    except (ValueError, TypeError):
        return False
    payload = {k: v for k, v in msg.items() if k != "sig"}
    return verify(canonical_json(payload), sig, pk)


# ── persistence: keypair + TOFU pin store ────────────────────────────────────

def d2a_home() -> Path:
    """
    Base dir for keys and pins. Override precedence:
      D2A_HOME  → that dir directly (used by tests to point at a tmpdir)
      XDG_DATA_HOME → $XDG_DATA_HOME/d2a
      else → ~/.d2a
    """
    env = os.environ.get("D2A_HOME")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "d2a"
    return Path.home() / ".d2a"


def _keys_dir() -> Path:
    return d2a_home() / "keys"


def _write_private(path: Path, data: dict) -> None:
    """Write a keypair file with 0600 perms, regardless of umask."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    blob = json.dumps(data, indent=2).encode("utf-8")
    # O_CREAT with mode 0600, then chmod to be certain even if the file pre-existed.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def load_or_create_keypair(name: str) -> KeyPair:
    """
    Load this node's persisted Ed25519 keypair, or create + persist one.

    Keyed by `name`, so re-running the same named node yields a STABLE identity
    across restarts — which is what makes TOFU pinning meaningful. node_id is
    always re-derived from the public key on load (defensive: a tampered
    node_id field in the file is ignored in favour of the real derivation).
    """
    path = _keys_dir() / f"{name}.json"
    if path.exists():
        data = json.loads(path.read_text())
        private_key = data["private_key"]
        public_key = data.get("public_key") or public_from_private(private_key)
        node_id = derive_node_id(public_key)   # never trust a stored node_id
        return KeyPair(node_id=node_id, private_key=private_key, public_key=public_key)

    private_key, public_key = generate_keypair()
    node_id = derive_node_id(public_key)
    _write_private(path, {
        "version": 1,
        "algo": "ed25519",
        "name": name,
        "private_key": private_key,   # 32-byte seed hex
        "public_key": public_key,
        "node_id": node_id,
    })
    return KeyPair(node_id=node_id, private_key=private_key, public_key=public_key)


class PinStore:
    """
    Trust-on-first-use pin store: node_id → public_key hex. Used by BOTH roles
    (agents pin the devices they discover; devices pin the agents that bind).

    verify() enforces two INDEPENDENT checks with distinct reasons:
      1. derivation — node_id must derive from the presented public key
         (ERR_DERIVATION). Stateless; catches a peer claiming a node_id that
         isn't bound to its key.
      2. pin equality — a previously seen node_id presenting a DIFFERENT key
         is rejected loudly (ERR_PIN); a first sighting is pinned and accepted.

    Persisted to <d2a_home>/known_peers.json (plus in-memory mirror) so pins
    survive restarts — otherwise every reboot would false-positive as a change.
    """

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else (d2a_home() / "known_peers.json")
        self._pins: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._pins = dict(json.loads(self.path.read_text()))
            except (ValueError, OSError):
                self._pins = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._pins, indent=2, sort_keys=True))

    def pinned_key(self, node_id: str) -> str | None:
        return self._pins.get(node_id)

    def verify(self, node_id: str, public_hex: str) -> str | None:
        """
        Return None if the identity is acceptable (pinning it on first sight),
        or an error reason (ERR_DERIVATION / ERR_PIN) if it must be rejected.
        """
        if not identity_matches(node_id, public_hex):
            return ERR_DERIVATION
        existing = self._pins.get(node_id)
        if existing is None:
            self._pins[node_id] = public_hex
            self._save()
            return None
        if existing != public_hex:
            return ERR_PIN
        return None
