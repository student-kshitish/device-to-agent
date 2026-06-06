"""
agents/guardian_agent.py — GuardianAgent: the borrowed brain for dumb relays.

KEY PRINCIPLE: The relay is a dumb nerve ending — it only drives the physical
device and relays raw bytes.  ALL intelligence lives here, in the guardian
agent, OUTSIDE the hardware.  dumb hardware + borrowed brain = virtual smart
object.  D2A is the nerve between them.

The guardian runs ANYWHERE — remote from the device, on a server, cloud, or
peer node.  Its location is irrelevant; it reaches the hardware through D2A
relay primitives only.  The skill set offered depends on the device kind:

  block_fs    → index, search, backup, organize        (filesystem intelligence)
  char_stream → collect, parse, tail                   (stream structuring)
  input_event → decode_events                          (physical-control decoding)
  sensor_file → monitor, verdict                       (sense-layer analytics)
  raw_generic → hexdump, capture                       (raw device inspection)

INPUT-EVENT NOTE: decode_events turns raw struct input_event records into
human-readable control actions (button pressed, axis moved, etc.).  This is
framed as PHYSICAL CONTROL INPUT for agents — game controllers, button boxes,
barcode scanners, assistive / adaptive devices, robot teleoperation.  NOT
for keyboard/mouse capture.  The relay enforces consent-gating; the guardian
surfaces consent errors clearly if consent is absent.

Pure stdlib — no external dependencies.
"""

import struct
import time
import uuid

from d2a.guardian.device_kinds import (
    KIND_BLOCK_FS,
    KIND_CHAR_STREAM,
    KIND_INPUT_EVENT,
    KIND_SENSOR_FILE,
    KIND_RAW_GENERIC,
    KIND_UNAVAILABLE,
)

# Default skills per device kind — set automatically by attach() if no override
_KIND_DEFAULT_SKILLS: dict[str, list[str]] = {
    KIND_BLOCK_FS:    ["index", "search", "backup", "organize"],
    KIND_CHAR_STREAM: ["collect", "parse", "tail"],
    KIND_INPUT_EVENT: ["decode_events"],
    KIND_SENSOR_FILE: ["monitor", "verdict"],
    KIND_RAW_GENERIC: ["hexdump", "capture"],
    KIND_UNAVAILABLE: [],
}

# Linux 64-bit struct input_event layout
_EV_FMT  = "<QQHHi"
_EV_SIZE = struct.calcsize(_EV_FMT)   # 24 bytes

# EV_* type constants (subset sufficient for control-input decoding)
_EV_TYPES = {0: "EV_SYN", 1: "EV_KEY", 2: "EV_REL", 3: "EV_ABS", 4: "EV_MSC"}
_KEY_VALS = {0: "released", 1: "pressed", 2: "held"}


class GuardianAgent:
    """
    Supplies intelligence to a dumb relay by composing PRIMITIVE relay ops
    into SMART skills.  Runs independently of the hardware.

    attach() determines the device kind from the relay's capability record
    and configures the appropriate skill set automatically.  A caller-supplied
    skills list overrides this auto-selection.
    """

    def __init__(self, name: str, skills: list[str] | None = None):
        self.name      = name
        self.agent_id  = str(uuid.uuid4())[:12]
        self._skills_override = skills   # None = auto-select from kind in attach()
        self.skills: list[str] = skills if skills is not None else []

        # State — populated by attach()
        self._relay        = None
        self._relay_cap: dict = {}
        self._bound_token: str | None = None
        self._kind         = KIND_UNAVAILABLE

        # Per-kind state buckets
        self._index:  dict = {}       # block_fs: {rel_path: {size, mtime}}
        self._buffer: bytes = b""     # char_stream: raw accumulated bytes

    # ── binding ───────────────────────────────────────────────────────────────

    def attach(self, relay_capability_record: dict) -> dict:
        """
        Bind to a relay's raw capability via the D2A bind flow.

        relay_capability_record: a dict from relay.capabilities()[i].
        In single-process mode it carries a 'relay_ref' key pointing to the
        relay object.  In distributed mode 'relay_ref' is absent and the
        caller wires up D2A TCP transport — only the transport changes, not
        the guardian logic.

        Auto-selects skills from the relay's kind unless an explicit skills
        list was provided at construction time.
        """
        self._relay_cap   = relay_capability_record
        self._relay       = relay_capability_record.get("relay_ref")
        self._kind        = relay_capability_record.get("kind", KIND_BLOCK_FS)
        self._bound_token = f"guardian-token-{self.agent_id}-{time.time():.0f}"
        self._index       = {}
        self._buffer      = b""

        if self._skills_override is None:
            self.skills = list(_KIND_DEFAULT_SKILLS.get(self._kind, []))

        return {
            "status":     "bound",
            "guardian":   self.name,
            "relay":      relay_capability_record.get("relay_node_id"),
            "capability": relay_capability_record.get("name"),
            "kind":       self._kind,
            "skills":     self.skills,
            "token":      self._bound_token,
        }

    def _call_relay(self, op: dict) -> dict:
        """Send a primitive op to the attached relay (in-process shortcut)."""
        if self._relay is None:
            return {"error": "not_attached"}
        return self._relay.handle_op(op)

    # ══════════════════════════════════════════════════════════════════════════
    # BLOCK_FS SKILLS — filesystem intelligence (index lives in guardian)
    # ══════════════════════════════════════════════════════════════════════════

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
            "index_lives": "guardian",
            "paths":       list(self._index.keys()),
        }

    def search(self, query: str) -> dict:
        """Query the guardian's in-memory index (substring match on path names)."""
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
            "searched_in": "guardian_index",
        }

    def backup(self, target_relay) -> dict:
        """
        Device-to-device copy orchestrated entirely by the guardian.
        Reads from the source relay, writes to target_relay — using only
        primitives.  Neither device knows about the other.
        """
        if "backup" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "backup"}
        if not self._index:
            return {"error": "index_empty", "hint": "call index() first"}

        copied = 0
        errors: list[str] = []

        for path, meta in self._index.items():
            size     = meta.get("size", 0)
            all_data = b""
            read_ok  = True

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

    def organize(self) -> dict:
        """
        Propose and apply a tidy structure using move = read+write+delete.
        Groups flat top-level files by extension into subdirectories.
        All structural reasoning is here; the device executes raw ops.
        """
        if "organize" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "organize"}
        if not self._index:
            return {"error": "index_empty", "hint": "call index() first"}

        moves: list[dict] = []

        for path in list(self._index.keys()):
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
            self.index()

        return {"moved": len(moves), "moves": moves}

    # ══════════════════════════════════════════════════════════════════════════
    # CHAR_STREAM SKILLS — stream structuring (GPS, serial sensor, scanner, …)
    # ══════════════════════════════════════════════════════════════════════════

    def collect(self, duration: float = 2.0) -> dict:
        """
        Open the stream and accumulate incoming bytes for up to *duration* seconds.

        On a real serial device bytes trickle in over time; on a simulated file
        all bytes are available immediately and the loop exits on consecutive
        empty reads.  The collected bytes are stored in self._buffer for parse()
        and tail() to consume.
        """
        if "collect" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "collect"}

        r = self._call_relay({"op": "open_stream"})
        if "error" in r:
            return r

        chunks:           list[bytes] = []
        deadline          = time.time() + duration
        consecutive_empty = 0

        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            r = self._call_relay({
                "op": "read_stream", "max_bytes": 65536,
                "timeout": min(0.5, remaining),
            })
            if "error" in r:
                break
            br = r.get("bytes_read", 0)
            if br > 0:
                chunks.append(bytes.fromhex(r["data"]))
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break   # EOF / quiet — no more data expected

        self._call_relay({"op": "close_stream"})
        self._buffer = b"".join(chunks)
        return {"bytes_collected": len(self._buffer), "ok": True}

    def parse(self, pattern: str = "nmea") -> dict:
        """
        Parse structured records from the collected buffer.

        pattern="nmea" : split NMEA-0183 sentences (lines starting with '$').
        Any other value : generic newline split.

        Intelligence lives here (in the guardian); the relay delivered raw bytes.
        """
        if "parse" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "parse"}
        if not self._buffer:
            return {"error": "no_data", "hint": "call collect() first"}

        text = self._buffer.decode("ascii", errors="replace")

        if pattern == "nmea":
            lines = [l.strip() for l in text.splitlines() if l.strip().startswith("$")]
            records = [
                {
                    "sentence": line,
                    "talker":   line[1:3] if len(line) > 3 else "?",
                    "msg_type": line[3:6] if len(line) > 6 else "?",
                }
                for line in lines
            ]
        else:
            lines   = [l.strip() for l in text.splitlines() if l.strip()]
            records = [{"line": l} for l in lines]

        return {"records": records, "count": len(records), "pattern": pattern}

    def tail(self) -> dict:
        """
        Return the latest line from the buffer, or perform a fresh single read
        if the buffer is empty.
        """
        if "tail" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "tail"}

        if self._buffer:
            data = self._buffer
        else:
            r = self._call_relay({"op": "open_stream"})
            if "error" in r:
                return r
            r2 = self._call_relay({"op": "read_stream", "max_bytes": 4096, "timeout": 0.5})
            self._call_relay({"op": "close_stream"})
            if "error" in r2 or r2.get("bytes_read", 0) == 0:
                return {"latest": None, "ok": True}
            data = bytes.fromhex(r2["data"])

        lines = [l.strip() for l in data.decode("utf-8", errors="replace").splitlines() if l.strip()]
        return {"latest": lines[-1] if lines else None, "ok": True}

    # ══════════════════════════════════════════════════════════════════════════
    # INPUT_EVENT SKILL — physical-control input decoding
    # ══════════════════════════════════════════════════════════════════════════

    def decode_events(self) -> dict:
        """
        Turn raw struct input_event records into readable CONTROL ACTIONS.

        PURPOSE: physical-control input for agents — game controllers, button
        boxes, barcode scanners, assistive / adaptive devices, robot
        teleoperation.  NOT intended for general keyboard/mouse capture.

        The relay delivers raw 24-byte records; the BRAIN (this method) maps
        type/code/value to human-readable action strings.  The relay has no
        idea what the bytes mean.

        Returns consent_required if the relay withheld the events.
        """
        if "decode_events" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "decode_events"}

        r = self._call_relay({"op": "read_events", "max_events": 64, "timeout": 1.0})
        if "error" in r:
            return r   # propagates consent_required transparently

        actions: list[str] = []
        for rec_hex in r.get("events", []):
            try:
                raw = bytes.fromhex(rec_hex)
                if len(raw) < _EV_SIZE:
                    actions.append("malformed_event")
                    continue
                _sec, _usec, ev_type, ev_code, ev_value = struct.unpack(_EV_FMT, raw[:_EV_SIZE])

                if ev_type == 1:   # EV_KEY — button / key state change
                    state = _KEY_VALS.get(ev_value, f"state_{ev_value}")
                    actions.append(f"button_{ev_code} {state}")
                elif ev_type == 2:  # EV_REL — relative axis (trackball, scroll)
                    axis = {0: "x", 1: "y", 2: "z", 6: "wheel"}.get(ev_code, f"rel_{ev_code}")
                    actions.append(f"rel_axis {axis} delta {ev_value:+d}")
                elif ev_type == 3:  # EV_ABS — absolute axis (analog stick, trigger)
                    actions.append(f"abs_axis_{ev_code} = {ev_value}")
                elif ev_type == 0:  # EV_SYN — event batch boundary
                    actions.append("sync")
                else:
                    type_name = _EV_TYPES.get(ev_type, f"ev_{ev_type}")
                    actions.append(f"{type_name} code={ev_code} val={ev_value}")
            except Exception:
                actions.append("malformed_event")

        return {
            "actions": actions,
            "count":   len(actions),
            "kind":    "control_input",
            "note":    "physical control input — not keyboard capture",
        }

    # ══════════════════════════════════════════════════════════════════════════
    # SENSOR_FILE SKILLS — sense-layer analytics
    # ══════════════════════════════════════════════════════════════════════════

    def monitor(self, intervals: int = 5, delay: float = 0.1) -> dict:
        """
        Poll read_value() *intervals* times (spaced by *delay* seconds) and
        return a time-series list.  Intelligence lives here; relay just reads.
        """
        if "monitor" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "monitor"}

        series: list[dict] = []
        for _ in range(intervals):
            r = self._call_relay({"op": "read_value"})
            if "error" not in r:
                series.append({"value": r.get("value"), "ts": time.time()})
            if delay > 0:
                time.sleep(delay)

        return {"series": series, "count": len(series)}

    def verdict(
        self,
        warn_threshold:   float = 75.0,
        danger_threshold: float = 90.0,
    ) -> dict:
        """
        Read the current sensor value and apply a simple threshold rule
        (sense-layer style): good / caution / danger.

        The relay delivers the raw string; the BRAIN (here) decides the verdict.
        """
        if "verdict" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "verdict"}

        r = self._call_relay({"op": "read_value"})
        if "error" in r:
            return r

        raw = r.get("value", "").strip()
        try:
            value = float(raw)
        except ValueError:
            return {"error": "non_numeric_value", "raw": raw}

        if value >= danger_threshold:
            level = "danger"
        elif value >= warn_threshold:
            level = "caution"
        else:
            level = "good"

        return {
            "value":            value,
            "level":            level,
            "warn_threshold":   warn_threshold,
            "danger_threshold": danger_threshold,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # RAW_GENERIC SKILLS — raw device inspection
    # ══════════════════════════════════════════════════════════════════════════

    def hexdump(self, length: int = 64) -> dict:
        """Hex-dump the first *length* bytes of the raw device."""
        if "hexdump" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "hexdump"}

        r = self._call_relay({"op": "read_bytes", "offset": 0, "length": length})
        if "error" in r:
            return r

        data  = bytes.fromhex(r.get("data", ""))
        lines: list[str] = []
        for i in range(0, len(data), 16):
            chunk     = data[i: i + 16]
            hex_part  = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{i:04x}  {hex_part:<47}  |{ascii_part}|")
        return {"hexdump": "\n".join(lines), "bytes_shown": len(data)}

    def capture(self, length: int = 256) -> dict:
        """Capture *length* raw bytes from the generic device."""
        if "capture" not in self.skills:
            return {"error": "skill_not_enabled", "skill": "capture"}

        r = self._call_relay({"op": "read_bytes", "offset": 0, "length": length})
        if "error" in r:
            return r
        return {"data": r.get("data"), "bytes_read": r.get("bytes_read", 0)}

    # ── introspection ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "name":        self.name,
            "agent_id":    self.agent_id,
            "kind":        self._kind,
            "skills":      self.skills,
            "index_size":  len(self._index),
            "attached_to": self._relay_cap.get("relay_node_id"),
        }
