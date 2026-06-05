"""
d2a/guardian/relay.py — DumbRelay: a brainless peripheral exposer.

KEY PRINCIPLE: The relay is a dumb nerve ending — it only drives the physical
device and relays raw bytes. ALL intelligence (indexing, search, backup,
encryption, organization) lives in the guardian agent, OUTSIDE the hardware.
dumb hardware + borrowed brain = virtual smart object. D2A is the nerve between them.

This entire file could run unchanged on a $5 resource-constrained embedded host:
  - no network stack required beyond the D2A transport
  - no Python packages beyond stdlib
  - the only I/O is to the raw device path the OS gives us
"""

import os
import shutil
import stat
from pathlib import Path


class DumbRelay:
    """
    Exposes a raw peripheral over D2A with NO logic beyond drive+relay.

    Device-agnostic: accepts any path the host provides — a mounted block
    device, a tmpfs directory, or any filesystem path.  The relay never makes
    assumptions about what kind of device is attached; it discovers the kind
    at runtime via plain stat() calls.

    Could run on a $5 embedded host unchanged.
    """

    def __init__(self, node_id: str, device_path_or_probe):
        """
        node_id: unique identifier for this relay node in the D2A mesh.
        device_path_or_probe: a filesystem path (str/Path) pointing at the
        device root, OR a zero-arg callable that returns such a path.
        Device-agnostic: the relay works with whatever raw path the host probes.
        """
        self.node_id = node_id

        path = device_path_or_probe() if callable(device_path_or_probe) else device_path_or_probe
        self._root = str(Path(path).resolve()) if path else None

    # ── device introspection (pure stat, no I/O to contents) ─────────────────

    def _device_kind(self) -> str:
        """Detect device type from path without hardcoding any device type."""
        if self._root is None:
            return "unknown"
        try:
            mode = os.stat(self._root).st_mode
            if stat.S_ISBLK(mode):
                return "block"
            if stat.S_ISDIR(mode):
                return "fs"
            if stat.S_ISREG(mode):
                return "raw_file"
            if stat.S_ISCHR(mode):
                return "char"
            return "unknown"
        except OSError:
            return "unavailable"

    def _device_available(self) -> bool:
        """Check device is still accessible — handles unplugged devices."""
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
        never hardcoded.  Returns a list (one entry per relay) describing the
        raw resource exactly as probed.  The 'relay_ref' key holds a direct
        object reference for in-process (single-process) use; in a real
        distributed deployment the caller uses D2A TCP transport instead.
        """
        if not self._device_available():
            return []

        kind = self._device_kind()

        try:
            if kind in ("fs", "block"):
                usage = shutil.disk_usage(self._root)
                return [{
                    "name":          "raw_storage",
                    "kind":          "block_or_fs",
                    "path":          self._root,
                    "size_bytes":    usage.total,
                    "free_bytes":    usage.free,
                    "writable":      os.access(self._root, os.W_OK),
                    "relay_node_id": self.node_id,
                    "relay_ref":     self,
                }]
            if kind == "char":
                return [{
                    "name":          "raw_stream",
                    "kind":          "char_device",
                    "path":          self._root,
                    "writable":      os.access(self._root, os.W_OK),
                    "relay_node_id": self.node_id,
                    "relay_ref":     self,
                }]
            return [{
                "name":          f"raw_{kind}",
                "kind":          kind,
                "path":          self._root,
                "writable":      os.access(self._root, os.W_OK),
                "relay_node_id": self.node_id,
                "relay_ref":     self,
            }]
        except OSError:
            return []

    # ── path sandbox ──────────────────────────────────────────────────────────

    def _safe_path(self, rel_path: str) -> str | None:
        """
        Resolve a caller-supplied path within the device root.
        Returns None if the resolved path escapes the device root.
        Uses realpath so symlink attacks are blocked too.
        """
        # Strip any leading slash so callers can pass "/" or "" for root
        norm      = os.path.normpath(rel_path.lstrip("/") or ".")
        candidate = os.path.realpath(os.path.join(self._root, norm))
        root_real = os.path.realpath(self._root)
        if candidate == root_real or candidate.startswith(root_real + os.sep):
            return candidate
        return None

    # ── op dispatcher ─────────────────────────────────────────────────────────

    def handle_op(self, op: dict) -> dict:
        """
        Execute a PRIMITIVE byte/file operation.

        Supported ops: list_entries, read_bytes, write_bytes, stat, delete.
        NO indexing, NO search, NO encryption — those are the guardian's job.
        Every path is sandboxed to the device root; escapes are rejected.

        Returns {"error": "device_unavailable"} if the device has been removed.
        """
        if not self._device_available():
            return {"error": "device_unavailable"}

        opname = op.get("op", "")
        try:
            if opname == "list_entries":
                return self._op_list_entries(op)
            if opname == "read_bytes":
                return self._op_read_bytes(op)
            if opname == "write_bytes":
                return self._op_write_bytes(op)
            if opname == "stat":
                return self._op_stat(op)
            if opname == "delete":
                return self._op_delete(op)
            return {"error": f"unknown_op:{opname}"}
        except OSError as exc:
            return {"error": f"os_error:{exc.errno}", "detail": str(exc)}
        except Exception as exc:
            return {"error": "relay_error", "detail": str(exc)}

    # ── primitive ops (thin wrappers over stdlib os/io) ───────────────────────

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

    def _op_read_bytes(self, op: dict) -> dict:
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
        return {
            "data":       data.hex(),
            "bytes_read": len(data),
            "offset":     offset,
            "path":       path,
        }

    def _op_write_bytes(self, op: dict) -> dict:
        path      = op.get("path", "")
        offset    = int(op.get("offset", 0))
        data_hex  = op.get("data", "")
        abs_path  = self._safe_path(path)
        if abs_path is None:
            return {"error": "path_sandbox_violation", "path": path}
        data = bytes.fromhex(data_hex) if data_hex else b""
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
