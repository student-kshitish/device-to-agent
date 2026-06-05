"""
agents/guardian_agent.py — GuardianAgent: the borrowed brain for dumb relays.

KEY PRINCIPLE: The relay is a dumb nerve ending — it only drives the physical
device and relays raw bytes. ALL intelligence (indexing, search, backup,
encryption, organization) lives here, in the guardian agent, OUTSIDE the hardware.
dumb hardware + borrowed brain = virtual smart object. D2A is the nerve between them.

The guardian runs ANYWHERE — remote from the device, on a server, cloud, or
peer node. Its location is irrelevant; it reaches the hardware through D2A
relay primitives only (list_entries, read_bytes, write_bytes, stat, delete).

Pure stdlib — no external dependencies.
"""

import time
import uuid


class GuardianAgent:
    """
    Supplies intelligence to a dumb relay by composing PRIMITIVE relay ops
    into SMART skills.  Runs independently of the hardware.

    Skills are pluggable: adding a new skill = adding a new method.
    Default set: index, search, backup, organize.  Encryption is optional/flagged.
    """

    def __init__(self, name: str, skills: list[str] | None = None):
        self.name     = name
        self.agent_id = str(uuid.uuid4())[:12]
        self.skills: list[str] = (
            skills if skills is not None
            else ["index", "search", "backup", "organize"]
        )

        # The index lives here, in the guardian — NOT on the device.
        self._index: dict = {}      # {rel_path: {size, mtime}}
        self._relay        = None   # relay object (in-process mode)
        self._relay_cap: dict = {}  # capability record we guard
        self._bound_token: str | None = None

    # ── binding ───────────────────────────────────────────────────────────────

    def attach(self, relay_capability_record: dict) -> dict:
        """
        Bind to a relay's raw capability via the D2A bind flow (scoped token).

        relay_capability_record: a dict from relay.capabilities()[i].
        In single-process mode it carries a 'relay_ref' key pointing to the
        relay object.  In distributed mode 'relay_ref' is absent and the
        caller wires up D2A TCP transport instead — only the transport changes,
        not the guardian logic.
        """
        self._relay_cap  = relay_capability_record
        self._relay      = relay_capability_record.get("relay_ref")
        self._bound_token = f"guardian-token-{self.agent_id}-{time.time():.0f}"
        self._index       = {}

        return {
            "status":     "bound",
            "guardian":   self.name,
            "relay":      relay_capability_record.get("relay_node_id"),
            "capability": relay_capability_record.get("name"),
            "token":      self._bound_token,
        }

    def _call_relay(self, op: dict) -> dict:
        """Send a primitive op to the attached relay (in-process shortcut)."""
        if self._relay is None:
            return {"error": "not_attached"}
        return self._relay.handle_op(op)

    # ── SKILL: index ──────────────────────────────────────────────────────────

    def index(self) -> dict:
        """
        Walk the device via list_entries+stat primitives and build an
        in-guardian index {path: {size, mtime}}.

        The index lives in the GUARDIAN, not on the device — the device has
        no idea it is being indexed.
        """
        if "index" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "index"}

        self._index = {}

        def _walk(rel_dir: str) -> None:
            result = self._call_relay({"op": "list_entries", "path": rel_dir})
            if "error" in result:
                return
            for entry in result.get("entries", []):
                prefix   = (rel_dir.rstrip("/") + "/") if rel_dir else ""
                rel_path = prefix + entry["name"]
                if entry.get("is_dir"):
                    _walk(rel_path)
                else:
                    s = self._call_relay({"op": "stat", "path": rel_path})
                    if "error" not in s:
                        self._index[rel_path] = {
                            "size":  s.get("size", 0),
                            "mtime": s.get("mtime", 0.0),
                        }

        _walk("")
        return {
            "indexed":     len(self._index),
            "index_lives": "guardian",  # explicitly NOT on the device
            "paths":       list(self._index.keys()),
        }

    # ── SKILL: search ─────────────────────────────────────────────────────────

    def search(self, query: str) -> dict:
        """
        Query the guardian's in-memory index (exact/substring match).

        Fuzzy or semantic (ML/embedding-based) search is a future option — not
        needed here; substring match over filenames is sufficient for this layer.
        """
        if "search" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "search"}
        if not self._index:
            return {"error": "index_empty", "hint": "call index() first"}
        q       = query.lower()
        matches = [p for p in self._index if q in p.lower()]
        return {
            "query":       query,
            "matches":     matches,
            "count":       len(matches),
            "searched_in": "guardian_index",  # proof: intelligence lives in guardian
        }

    # ── SKILL: backup ─────────────────────────────────────────────────────────

    def backup(self, target_relay) -> dict:
        """
        Device-to-device copy orchestrated entirely by the guardian.
        Reads from the source relay, writes to target_relay — using only
        primitives.  Neither device knows about the other; the guardian is
        the sole orchestrator.
        """
        if "backup" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "backup"}
        if not self._index:
            return {"error": "index_empty", "hint": "call index() first"}

        copied = 0
        errors: list[str] = []

        for path, meta in self._index.items():
            size      = meta.get("size", 0)
            all_data  = b""
            read_ok   = True

            if size > 0:
                offset, chunk = 0, 4096
                while offset < size:
                    to_read = min(chunk, size - offset)
                    r = self._call_relay({
                        "op": "read_bytes", "path": path,
                        "offset": offset, "length": to_read,
                    })
                    if "error" in r:
                        errors.append(f"{path}: read error: {r['error']}")
                        read_ok = False
                        break
                    chunk_bytes = bytes.fromhex(r.get("data", ""))
                    all_data   += chunk_bytes
                    advanced    = r.get("bytes_read", len(chunk_bytes))
                    offset     += advanced
                    if advanced == 0:
                        break

            if read_ok:
                w = target_relay.handle_op({
                    "op": "write_bytes", "path": path,
                    "offset": 0, "data": all_data.hex(),
                })
                if "error" in w:
                    errors.append(f"{path}: write error: {w['error']}")
                else:
                    copied += 1

        return {"copied": copied, "errors": errors, "ok": len(errors) == 0}

    # ── SKILL: organize ───────────────────────────────────────────────────────

    def organize(self) -> dict:
        """
        Propose and apply a tidy structure using move = read+write+delete.
        Groups flat top-level files by extension into subdirectories.
        The device executes raw ops; all structural reasoning is here.
        """
        if "organize" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "organize"}
        if not self._index:
            return {"error": "index_empty", "hint": "call index() first"}

        moves: list[dict] = []

        for path in list(self._index.keys()):
            # Only reorganize flat top-level files
            if "/" in path:
                continue
            filename = path
            ext      = filename.rsplit(".", 1)[-1].lower() if "." in filename else "misc"
            new_path = f"{ext}/{filename}"

            s = self._call_relay({"op": "stat", "path": path})
            if "error" in s:
                continue
            size = s.get("size", 0)

            r = self._call_relay({
                "op": "read_bytes", "path": path, "offset": 0, "length": max(size, 1),
            })
            if "error" in r:
                continue

            w = self._call_relay({
                "op": "write_bytes", "path": new_path,
                "offset": 0, "data": r.get("data", ""),
            })
            if "error" in w:
                continue

            d = self._call_relay({"op": "delete", "path": path})
            if "error" not in d:
                moves.append({"from": path, "to": new_path})

        if moves:
            self.index()  # refresh index after reorganization

        return {"moved": len(moves), "moves": moves}

    # ── introspection ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "name":        self.name,
            "agent_id":    self.agent_id,
            "skills":      self.skills,
            "index_size":  len(self._index),
            "attached_to": self._relay_cap.get("relay_node_id"),
        }
