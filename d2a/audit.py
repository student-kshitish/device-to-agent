"""
d2a/audit.py — PHASE 8: the signed, append-only, hash-chained intervention audit.

The intervention layer trades owner-approval-of-a-PLAN and a tamper-evident audit
trail for the git safety net that ordinary code edits enjoy. This module is that
trail: every terminal outcome of a propose_intervention — approved+executed,
owner-DENIED, or failed-verify — is written here as ONE device-signed line.

TAMPER-EVIDENCE (append-only without OS enforcement): each entry carries the
sha256 of the previous entry's canonical signed bytes in a `prev_hash` field that
is itself inside the signed payload. So the log is a hash chain, and the device
host key signs each link. Rewriting or truncating any past line breaks either a
signature or the chain — detectable by verify_chain(). The device is FAIL-CLOSED:
before extending the log it re-verifies the whole chain, and REFUSES to append on
top of a tampered tail (a compromised log must not be silently continued).

Survives restart: it is a file at d2a_home()/audit/<device>.jsonl (same base dir,
overrides, and 0600/0700 perms as keys/pins). On the next append after a restart
the device reads + verifies the existing chain and continues from its head.

LEAF-ish: stdlib + d2a.crypto only (the same Ed25519 + canonical-JSON the rest of
the trust layer uses). No transport, no runtime import.
"""

import hashlib
import json
import os

from d2a import crypto


class AuditError(Exception):
    """Raised when the log cannot be safely extended — most importantly when the
    existing chain fails verification (fail-closed: never append over a tamper)."""


def _audit_dir() -> "os.PathLike":
    d = crypto.d2a_home() / "audit"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _entry_hash(entry: dict) -> str:
    """sha256 of an entry's canonical bytes (INCLUDING its sig), hex. This is the
    value the NEXT entry stores as prev_hash — so the chain covers the signature."""
    return hashlib.sha256(crypto.canonical_json(entry)).hexdigest()


class AuditLog:
    """Device-owned append-only signed audit log for interventions."""

    def __init__(self, device_name: str, private_key: str, public_key: str) -> None:
        self.device_name = device_name
        self._priv = private_key
        self._pub  = public_key
        self.path  = _audit_dir() / f"{device_name}.jsonl"

    # ── read ────────────────────────────────────────────────────────────────────

    def _read_entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
        return out

    def verify_chain(self) -> tuple[bool, str]:
        """Re-verify the whole log: every line's device signature, the seq run
        (0,1,2,…), and each prev_hash link. Returns (ok, detail). An empty/missing
        log is a valid (empty) chain."""
        try:
            entries = self._read_entries()
        except (OSError, json.JSONDecodeError) as e:
            return False, f"unreadable/corrupt log: {e}"
        prev_hash = ""
        for i, e in enumerate(entries):
            if not crypto.verify_dict(e, self._pub):
                return False, f"entry seq={e.get('seq', i)} signature invalid (tampered)"
            if e.get("seq") != i:
                return False, f"seq gap at index {i}: got {e.get('seq')!r}"
            if e.get("prev_hash", None) != prev_hash:
                return False, f"broken chain at seq={i}: prev_hash mismatch"
            prev_hash = _entry_hash(e)
        return True, f"{len(entries)} entries, chain intact"

    def entries(self) -> list[dict]:
        """All parsed entries (for inspection/tests). Does not verify."""
        return self._read_entries()

    def head(self) -> dict | None:
        """The most recent entry, or None if the log is empty."""
        es = self._read_entries()
        return es[-1] if es else None

    # ── append ────────────────────────────────────────────────────────────────

    def append(self, fields: dict) -> dict:
        """
        Append ONE signed entry. FAIL-CLOSED: verifies the existing chain first and
        raises AuditError (writing nothing) if it is broken — the device refuses to
        extend a tampered log. Returns the signed entry that was written.

        `fields` are the caller-supplied audit payload (plan, approver, result,
        verify outcome, …); seq / prev_hash / device_node_id are set here.
        """
        ok, detail = self.verify_chain()
        if not ok:
            raise AuditError(f"refusing to extend audit log: {detail}")

        entries = self._read_entries()
        seq       = len(entries)
        prev_hash = _entry_hash(entries[-1]) if entries else ""

        entry = {
            **fields,
            "seq":            seq,
            "prev_hash":      prev_hash,
            "device_name":    self.device_name,
        }
        signed = crypto.sign_dict(entry, self._priv, self._pub)

        line = json.dumps(signed, separators=(",", ":"), ensure_ascii=False) + "\n"
        # O_APPEND + 0600. Append-only intent; tamper-evidence is the hash chain.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return signed
