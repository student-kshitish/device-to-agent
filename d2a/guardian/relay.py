"""
d2a/guardian/relay.py — DumbRelay: a brainless, device-agnostic peripheral exposer.

KEY PRINCIPLE: The relay is a dumb nerve ending — it only drives the physical
device and relays raw bytes/events.  ALL intelligence lives in the guardian
agent OUTSIDE the hardware.  dumb hardware + borrowed brain = virtual smart
object.  D2A is the nerve between them.

SECURITY CONTRACT:
- The relay ONLY exposes the SINGLE device path the user explicitly passes in.
  It NEVER auto-scans /dev or any directory.
- A realpath-based scope jail applies to block_fs / raw_generic.
- For char_stream / input_event / sensor_file the relay is scoped to the
  EXACT device path given — no other path is reachable via this relay.
- INPUT-EVENT devices are SENSITIVE (reading a live event stream is functionally
  a keylogger).  The relay requires consent_granted=True before exposing any
  input_event primitives.  Without it every op returns {"error":"consent_required"}.
  Legitimate use: game controllers, button boxes, barcode scanners, assistive /
  adaptive devices, robot teleoperation.  NOT general keyboard/mouse capture.
- Raw-device access requires appropriate OS permissions.  Real remote deployments
  MUST enforce strong trust (e.g., Ed25519 signing) before granting relay access.

This file contains ZERO hardcoded peripheral-type strings.
Everything is discovered at runtime via os.stat() mode bits and path patterns
in device_kinds.py.  Could run unchanged on a $5 embedded host.
"""

import os
import select
import shutil
import stat
import struct
from pathlib import Path

from d2a.guardian.device_kinds import (
    detect_kind,
    is_system_input,
    KIND_BLOCK_FS,
    KIND_CHAR_STREAM,
    KIND_INPUT_EVENT,
    KIND_SENSOR_FILE,
    KIND_RAW_GENERIC,
    KIND_UNAVAILABLE,
    KIND_SENSITIVITY,
    KIND_PRIMITIVES as _KIND_PRIMITIVES,  # canonical source; do not redefine here
)

# Canonical capability name per kind
_KIND_CAP_NAME: dict[str, str] = {
    KIND_BLOCK_FS:    "raw_block_fs",
    KIND_CHAR_STREAM: "raw_char_stream",
    KIND_INPUT_EVENT: "raw_input_event",
    KIND_SENSOR_FILE: "raw_sensor_file",
    KIND_RAW_GENERIC: "raw_generic",
    KIND_UNAVAILABLE: "raw_unavailable",
}

# Struct layout for Linux 64-bit struct input_event:
#   tv_sec (u64) + tv_usec (u64) + type (u16) + code (u16) + value (i32) = 24 bytes
_INPUT_EVENT_FMT  = "<QQHHi"
_INPUT_EVENT_SIZE = struct.calcsize(_INPUT_EVENT_FMT)   # 24 bytes


class DumbRelay:
    """
    Exposes a raw peripheral over D2A with NO logic beyond drive+relay.

    Generalised to any dumb peripheral:
      block_fs    — directory / mounted block device with a filesystem
      char_stream — serial / character device (GPS, Arduino, scanner, …)
      input_event — input-event node (controller, button box, …) [SENSITIVE]
      sensor_file — sysfs/proc scalar file (temperature, voltage, …)
      raw_generic — any other device node that doesn't match the above
      unavailable — path missing or unreadable

    The relay offers ONLY the primitive set valid for the detected kind.
    The guardian agent composes those primitives into smart skills.

    Designed to run on a $5 resource-constrained embedded host unchanged.
    """

    def __init__(
        self,
        node_id: str,
        device_path_or_probe,
        consent_granted: bool = False,
        kind_override: str | None = None,
        system_input_override: bool | None = None,
    ):
        """
        node_id             : unique identifier for this relay in the D2A mesh.
        device_path_or_probe: filesystem path (str/Path) or zero-arg callable that
                              returns one.  The relay only ever accesses THIS path.
        consent_granted     : must be True before any input_event primitive is served.
                              Default False — SENSITIVE kind is DENIED by default.
        kind_override       : skip auto-detection and force a specific kind string.
                              Intended for simulation / testing; real deployments omit it.
        system_input_override: force the system_input flag in the capability record.
                              Used in tests to simulate a keyboard-like device path.
        """
        self.node_id = node_id
        self._consent_granted = consent_granted

        path        = device_path_or_probe() if callable(device_path_or_probe) else device_path_or_probe
        self._root  = str(Path(path).resolve()) if path else None

        self._kind  = kind_override if kind_override else detect_kind(self._root or "")
        self._system_input_override = system_input_override

        # Stateful stream handle for char_stream open/read/write/close ops
        self._stream_fh = None

    # ── device availability ───────────────────────────────────────────────────

    def _device_available(self) -> bool:
        """Check device is still accessible — handles hot-unplugged devices gracefully."""
        if self._root is None:
            return False
        try:
            os.stat(self._root)
            return True
        except OSError:
            return False

    # ── capability advertisement ──────────────────────────────────────────────

    def capabilities(self) -> list:
        """
        Advertise the RAW capability of the device — discovered at runtime,
        never hardcoded.  Returns a single-element list with a capability dict.

        For input_event without consent: the device IS listed but access is
        "consent_required" and ALL ops return consent_required until granted.
        For unavailable devices: returns an empty list.

        The 'relay_ref' key is a direct object reference for in-process use.
        In a real distributed deployment the caller uses D2A TCP transport.
        """
        if self._kind == KIND_UNAVAILABLE or not self._device_available():
            return []

        primitives = _KIND_PRIMITIVES[self._kind]

        # Consent gate for sensitive kind
        is_sensitive = KIND_SENSITIVITY.get(self._kind) == "sensitive"
        access       = (
            "consent_required" if (is_sensitive and not self._consent_granted)
            else "open"
        )

        # system_input flag (meaningful only for input_event)
        if self._system_input_override is not None:
            sys_input = self._system_input_override
        else:
            sys_input = is_system_input(self._root) if self._kind == KIND_INPUT_EVENT else False

        cap: dict = {
            "name":          _KIND_CAP_NAME[self._kind],
            "kind":          self._kind,
            "path":          self._root,
            "primitives":    primitives,
            "access":        access,
            "system_input":  sys_input,
            "relay_node_id": self.node_id,
            "relay_ref":     self,
        }

        # Kind-specific live-state fields
        try:
            if self._kind == KIND_BLOCK_FS:
                usage          = shutil.disk_usage(self._root)
                cap["size_bytes"] = usage.total
                cap["free_bytes"] = usage.free
                cap["writable"]   = os.access(self._root, os.W_OK)
            elif self._kind in (KIND_CHAR_STREAM, KIND_INPUT_EVENT,
                                KIND_SENSOR_FILE, KIND_RAW_GENERIC):
                cap["readable"] = os.access(self._root, os.R_OK)
                cap["writable"] = os.access(self._root, os.W_OK)
        except OSError:
            pass

        return [cap]

    # ── path jails ───────────────────────────────────────────────────────────

    def _safe_path(self, rel_path: str) -> str | None:
        """
        Resolve a caller-supplied relative path within the device root (block_fs /
        raw_generic).  Returns None if the resolved path escapes the root.
        Realpath blocks symlink traversal attacks too.
        """
        norm      = os.path.normpath(rel_path.lstrip("/") or ".")
        candidate = os.path.realpath(os.path.join(self._root, norm))
        root_real = os.path.realpath(self._root)
        if candidate == root_real or candidate.startswith(root_real + os.sep):
            return candidate
        return None

    def _check_exact_path_jail(self, op: dict) -> dict | None:
        """
        Scope jail for char_stream / input_event / sensor_file.
        These kinds are scoped to the EXACT device path — no other path is
        reachable.  If the op contains a 'path' field that differs from the
        device root, return a path_sandbox_violation error dict.
        Ops without a 'path' field are implicitly allowed (they operate on the
        sole registered device).
        """
        if "path" not in op:
            return None
        supplied = str(op["path"])
        try:
            supplied_real = os.path.realpath(supplied)
        except OSError:
            supplied_real = supplied
        root_real = os.path.realpath(self._root) if self._root else ""
        if supplied_real != root_real:
            return {
                "error":   "path_sandbox_violation",
                "supplied": supplied,
                "allowed":  self._root,
            }
        return None

    # ── consent gate ─────────────────────────────────────────────────────────

    def _consent_gate(self) -> dict | None:
        """Return a consent_required error if this kind needs consent and it hasn't been granted."""
        if self._kind == KIND_INPUT_EVENT and not self._consent_granted:
            return {
                "error":   "consent_required",
                "message": (
                    "Input-event access requires explicit user consent. "
                    "Create the relay with consent_granted=True to enable. "
                    "Legitimate use: game controllers, button boxes, scanners, "
                    "assistive devices, robot teleoperation — NOT keyboard capture."
                ),
            }
        return None

    # ── op dispatcher ─────────────────────────────────────────────────────────

    def handle_op(self, op: dict) -> dict:
        """
        Execute a PRIMITIVE byte/file/stream operation.

        Supported ops depend on the device kind:
          block_fs    : list_entries, read_bytes, write_bytes, stat, delete
          char_stream : open_stream, read_stream(max_bytes,timeout), write_stream(data), close_stream
          input_event : read_events(max_events,timeout)   [consent required]
          sensor_file : read_value
          raw_generic : read_bytes(offset,length), write_bytes(offset,data)
          unavailable : every op → {"error":"device_unavailable"}

        NO indexing, NO search, NO encryption — those are the guardian's job.
        Every path is sandboxed; escapes are rejected.
        Unplugged device → device_unavailable, never crash.
        """
        if self._kind == KIND_UNAVAILABLE or not self._device_available():
            return {"error": "device_unavailable"}

        opname = op.get("op", "")

        try:
            # ── block_fs ops ─────────────────────────────────────────────────
            if self._kind == KIND_BLOCK_FS:
                if opname == "list_entries": return self._op_list_entries(op)
                if opname == "read_bytes":   return self._op_read_bytes_fs(op)
                if opname == "write_bytes":  return self._op_write_bytes_fs(op)
                if opname == "stat":         return self._op_stat(op)
                if opname == "delete":       return self._op_delete(op)
                return {"error": f"op_not_valid_for_kind:{opname}", "kind": self._kind,
                        "valid": _KIND_PRIMITIVES[self._kind]}

            # ── char_stream ops ───────────────────────────────────────────────
            if self._kind == KIND_CHAR_STREAM:
                jail = self._check_exact_path_jail(op)
                if jail: return jail
                if opname == "open_stream":  return self._op_open_stream(op)
                if opname == "read_stream":  return self._op_read_stream(op)
                if opname == "write_stream": return self._op_write_stream(op)
                if opname == "close_stream": return self._op_close_stream(op)
                return {"error": f"op_not_valid_for_kind:{opname}", "kind": self._kind,
                        "valid": _KIND_PRIMITIVES[self._kind]}

            # ── input_event ops (consent-gated) ───────────────────────────────
            if self._kind == KIND_INPUT_EVENT:
                gate = self._consent_gate()
                if gate: return gate
                jail = self._check_exact_path_jail(op)
                if jail: return jail
                if opname == "read_events": return self._op_read_events(op)
                return {"error": f"op_not_valid_for_kind:{opname}", "kind": self._kind,
                        "valid": _KIND_PRIMITIVES[self._kind]}

            # ── sensor_file ops ───────────────────────────────────────────────
            if self._kind == KIND_SENSOR_FILE:
                jail = self._check_exact_path_jail(op)
                if jail: return jail
                if opname == "read_value": return self._op_read_value(op)
                return {"error": f"op_not_valid_for_kind:{opname}", "kind": self._kind,
                        "valid": _KIND_PRIMITIVES[self._kind]}

            # ── raw_generic ops ───────────────────────────────────────────────
            if self._kind == KIND_RAW_GENERIC:
                if opname == "read_bytes":  return self._op_read_bytes_raw(op)
                if opname == "write_bytes": return self._op_write_bytes_raw(op)
                return {"error": f"op_not_valid_for_kind:{opname}", "kind": self._kind,
                        "valid": _KIND_PRIMITIVES[self._kind]}

        except OSError as exc:
            return {"error": f"os_error:{exc.errno}", "detail": str(exc)}
        except Exception as exc:
            return {"error": "relay_error", "detail": str(exc)}

        return {"error": f"unknown_kind:{self._kind}"}

    # ══════════════════════════════════════════════════════════════════════════
    # BLOCK_FS primitives (keep as-is from original — thin wrappers over stdlib)
    # ══════════════════════════════════════════════════════════════════════════

    def _op_list_entries(self, op: dict) -> dict:
        path     = op.get("path", "")
        abs_path = self._safe_path(path)
        if abs_path is None:
            return {"error": "path_sandbox_violation", "path": path}
        if not os.path.isdir(abs_path):
            return {"error": "not_a_directory", "path": path}
        entries = []
        for name in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, name)
            try:
                s = os.stat(full)
                entries.append({
                    "name":   name,
                    "is_dir": stat.S_ISDIR(s.st_mode),
                    "size":   s.st_size,
                    "mtime":  s.st_mtime,
                })
            except OSError:
                entries.append({"name": name, "error": "stat_failed"})
        return {"entries": entries, "count": len(entries)}

    def _op_read_bytes_fs(self, op: dict) -> dict:
        path     = op.get("path", "")
        offset   = int(op.get("offset", 0))
        length   = int(op.get("length", 4096))
        abs_path = self._safe_path(path)
        if abs_path is None:
            return {"error": "path_sandbox_violation", "path": path}
        if not os.path.isfile(abs_path):
            return {"error": "not_a_file", "path": path}
        with open(abs_path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(length)
        return {"data": data.hex(), "bytes_read": len(data), "offset": offset, "path": path}

    def _op_write_bytes_fs(self, op: dict) -> dict:
        path     = op.get("path", "")
        offset   = int(op.get("offset", 0))
        data_hex = op.get("data", "")
        abs_path = self._safe_path(path)
        if abs_path is None:
            return {"error": "path_sandbox_violation", "path": path}
        data   = bytes.fromhex(data_hex) if data_hex else b""
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        mode = "r+b" if os.path.exists(abs_path) else "wb"
        with open(abs_path, mode) as fh:
            fh.seek(offset)
            fh.write(data)
        return {"bytes_written": len(data), "path": path, "offset": offset}

    def _op_stat(self, op: dict) -> dict:
        path     = op.get("path", "")
        abs_path = self._safe_path(path)
        if abs_path is None:
            return {"error": "path_sandbox_violation", "path": path}
        if not os.path.exists(abs_path):
            return {"error": "not_found", "path": path}
        s = os.stat(abs_path)
        return {
            "path":    path,
            "size":    s.st_size,
            "mtime":   s.st_mtime,
            "is_dir":  stat.S_ISDIR(s.st_mode),
            "is_file": stat.S_ISREG(s.st_mode),
            "mode":    oct(s.st_mode),
        }

    def _op_delete(self, op: dict) -> dict:
        path     = op.get("path", "")
        abs_path = self._safe_path(path)
        if abs_path is None:
            return {"error": "path_sandbox_violation", "path": path}
        if not os.path.exists(abs_path):
            return {"error": "not_found", "path": path}
        if os.path.isdir(abs_path):
            shutil.rmtree(abs_path)
        else:
            os.unlink(abs_path)
        return {"deleted": path, "ok": True}

    # ══════════════════════════════════════════════════════════════════════════
    # CHAR_STREAM primitives — non-blocking with select-based timeout
    # ══════════════════════════════════════════════════════════════════════════

    def _op_open_stream(self, op: dict) -> dict:
        """Open the character device for byte-stream I/O."""
        if self._stream_fh is not None:
            return {"error": "stream_already_open", "hint": "call close_stream first"}
        try:
            self._stream_fh = open(self._root, "r+b", buffering=0)
            return {"status": "stream_opened", "path": self._root}
        except OSError as exc:
            return {"error": f"open_failed:{exc.errno}", "detail": str(exc)}

    def _op_read_stream(self, op: dict) -> dict:
        """
        Read up to max_bytes from the open stream, blocking for at most timeout seconds.
        Returns timeout=True if no data arrived within the window.
        """
        if self._stream_fh is None:
            return {"error": "stream_not_open", "hint": "call open_stream first"}
        max_bytes = int(op.get("max_bytes", 4096))
        timeout   = float(op.get("timeout", 1.0))
        try:
            ready, _, _ = select.select([self._stream_fh], [], [], timeout)
            if not ready:
                return {"data": "", "bytes_read": 0, "timeout": True}
            data = self._stream_fh.read(max_bytes)
            return {"data": data.hex(), "bytes_read": len(data), "timeout": False}
        except OSError as exc:
            return {"error": f"read_failed:{exc.errno}", "detail": str(exc)}

    def _op_write_stream(self, op: dict) -> dict:
        """Write raw bytes to the open stream."""
        if self._stream_fh is None:
            return {"error": "stream_not_open", "hint": "call open_stream first"}
        data_hex = op.get("data", "")
        try:
            data = bytes.fromhex(data_hex) if data_hex else b""
            self._stream_fh.write(data)
            self._stream_fh.flush()
            return {"bytes_written": len(data)}
        except OSError as exc:
            return {"error": f"write_failed:{exc.errno}", "detail": str(exc)}

    def _op_close_stream(self, op: dict) -> dict:
        """Close the stream."""
        if self._stream_fh is None:
            return {"error": "stream_not_open"}
        try:
            self._stream_fh.close()
        except OSError:
            pass
        finally:
            self._stream_fh = None
        return {"status": "stream_closed"}

    # ══════════════════════════════════════════════════════════════════════════
    # INPUT_EVENT primitives — reads raw struct input_event records
    # Guardian (not relay) interprets the records; relay delivers raw bytes.
    # ══════════════════════════════════════════════════════════════════════════

    def _op_read_events(self, op: dict) -> dict:
        """
        Read up to max_events raw input_event records from the device.

        Each record is 24 bytes (64-bit Linux struct input_event):
          tv_sec(u64) tv_usec(u64) type(u16) code(u16) value(i32).

        The relay delivers raw hex-encoded records.  The guardian agent
        (not the relay) is responsible for interpreting type/code/value.
        """
        max_events = int(op.get("max_events", 16))
        timeout    = float(op.get("timeout", 1.0))
        want_bytes = _INPUT_EVENT_SIZE * max_events
        try:
            with open(self._root, "rb") as fh:
                ready, _, _ = select.select([fh], [], [], timeout)
                if not ready:
                    return {"events": [], "count": 0, "timeout": True}
                data = fh.read(want_bytes)
            records = [
                data[i: i + _INPUT_EVENT_SIZE].hex()
                for i in range(0, len(data) - (_INPUT_EVENT_SIZE - 1), _INPUT_EVENT_SIZE)
            ]
            return {"events": records, "count": len(records), "timeout": False}
        except OSError as exc:
            return {"error": f"read_failed:{exc.errno}", "detail": str(exc)}

    # ══════════════════════════════════════════════════════════════════════════
    # SENSOR_FILE primitives — single fresh scalar read
    # ══════════════════════════════════════════════════════════════════════════

    def _op_read_value(self, op: dict) -> dict:
        """
        Read the scalar value from the sensor file (sysfs/proc or simulated).
        Returns raw text stripped of whitespace.
        """
        try:
            with open(self._root, "r", errors="replace") as fh:
                value = fh.read(4096).strip()
            return {"value": value, "path": self._root}
        except OSError as exc:
            return {"error": f"read_failed:{exc.errno}", "detail": str(exc)}

    # ══════════════════════════════════════════════════════════════════════════
    # RAW_GENERIC primitives — best-effort byte access on an unknown device
    # ══════════════════════════════════════════════════════════════════════════

    def _op_read_bytes_raw(self, op: dict) -> dict:
        """Read raw bytes from a generic device node."""
        offset = int(op.get("offset", 0))
        length = int(op.get("length", 4096))
        try:
            with open(self._root, "rb") as fh:
                fh.seek(offset)
                data = fh.read(length)
            return {"data": data.hex(), "bytes_read": len(data), "offset": offset}
        except OSError as exc:
            return {"error": f"read_failed:{exc.errno}", "detail": str(exc)}

    def _op_write_bytes_raw(self, op: dict) -> dict:
        """Write raw bytes to a generic device node."""
        offset   = int(op.get("offset", 0))
        data_hex = op.get("data", "")
        try:
            data = bytes.fromhex(data_hex) if data_hex else b""
            mode = "r+b" if os.path.exists(self._root) else "wb"
            with open(self._root, mode) as fh:
                fh.seek(offset)
                fh.write(data)
            return {"bytes_written": len(data), "offset": offset}
        except OSError as exc:
            return {"error": f"write_failed:{exc.errno}", "detail": str(exc)}
