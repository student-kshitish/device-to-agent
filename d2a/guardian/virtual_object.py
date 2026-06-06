"""
d2a/guardian/virtual_object.py — VirtualSmartObject: fusion of dumb relay + guardian brain.

KEY PRINCIPLE: The relay is a dumb nerve ending — it only drives the physical
device and relays raw bytes.  ALL intelligence lives in the guardian agent,
OUTSIDE the hardware.  dumb hardware + borrowed brain = virtual smart object.
D2A is the nerve between them.

From the network's perspective this object IS a smart device.  Under the hood:
  other agent → VirtualSmartObject → GuardianAgent skill → DumbRelay primitive → raw device

The advertised name, tags, and routing all adapt to the device kind automatically:
  block_fs    → "smart_storage"       [indexed, searchable, backup_capable, organized]
  char_stream → "smart_sensor_stream" [parsed, structured, tailable]
  input_event → "smart_control_input" [decoded, consent_required]
  sensor_file → "smart_sensor"        [monitored, verdict]
  raw_generic → "smart_raw_device"    [hexdump, capture]
"""

from d2a.guardian.device_kinds import (
    KIND_BLOCK_FS,
    KIND_CHAR_STREAM,
    KIND_INPUT_EVENT,
    KIND_SENSOR_FILE,
    KIND_RAW_GENERIC,
)

# Smart-object identity per kind
_SMART_NAME: dict[str, str] = {
    KIND_BLOCK_FS:    "smart_storage",
    KIND_CHAR_STREAM: "smart_sensor_stream",
    KIND_INPUT_EVENT: "smart_control_input",
    KIND_SENSOR_FILE: "smart_sensor",
    KIND_RAW_GENERIC: "smart_raw_device",
}

_SMART_TAGS: dict[str, list[str]] = {
    KIND_BLOCK_FS:    ["indexed", "searchable", "backup_capable", "organized",
                       "virtual_smart_object"],
    KIND_CHAR_STREAM: ["parsed", "structured", "tailable", "virtual_smart_object"],
    KIND_INPUT_EVENT: ["decoded", "consent_required", "virtual_smart_object"],
    KIND_SENSOR_FILE: ["monitored", "verdict", "virtual_smart_object"],
    KIND_RAW_GENERIC: ["hexdump", "capture", "virtual_smart_object"],
}


class VirtualSmartObject:
    """
    The fusion of (dumb relay capability) + (guardian agent skills) presented
    as ONE smart capability that other agents discover and use.

    To the rest of the D2A network this appears as an intelligent device.
    The underlying hardware is completely brainless — the guardian supplies
    all intelligence from outside the hardware boundary.

    The advertised name, tags, and available actions are determined by the
    device kind of the underlying relay.
    """

    def __init__(self, relay_record: dict, guardian):
        """
        relay_record: a capability dict from DumbRelay.capabilities()[i].
        guardian: a GuardianAgent already attached to the relay.
        """
        self.relay_record = relay_record
        self.guardian     = guardian
        self._kind        = relay_record.get("kind", KIND_BLOCK_FS)

    # ── network-facing advertisement ──────────────────────────────────────────

    def advertised_capability(self) -> dict:
        """
        Present as a SMART capability.  Name and tags reflect what the guardian
        adds on top of raw hardware.  live_state carries current device metrics.
        """
        kind = self._kind
        name = _SMART_NAME.get(kind, "smart_device")
        tags = list(_SMART_TAGS.get(kind, ["virtual_smart_object"]))

        live_state: dict = {}

        relay = self.relay_record.get("relay_ref")
        if relay:
            caps = relay.capabilities()
            if caps:
                cap = caps[0]
                if kind == KIND_BLOCK_FS:
                    live_state["entries_indexed"] = len(self.guardian._index)
                    live_state["free_bytes"]      = cap.get("free_bytes", 0)
                elif kind == KIND_CHAR_STREAM:
                    live_state["buffer_bytes"]    = len(self.guardian._buffer)
                elif kind == KIND_SENSOR_FILE:
                    live_state["readable"] = cap.get("readable", False)
                elif kind == KIND_INPUT_EVENT:
                    live_state["access"]       = cap.get("access", "consent_required")
                    live_state["system_input"] = cap.get("system_input", False)
                elif kind == KIND_RAW_GENERIC:
                    live_state["readable"] = cap.get("readable", False)
                    live_state["writable"] = cap.get("writable", False)

        return {
            "name":      name,
            "kind":      kind,
            "tags":      tags,
            "backed_by": self.relay_record.get("relay_node_id"),
            "guardian":  self.guardian.agent_id,
            "skills":    list(self.guardian.skills),
            "access":    self.relay_record.get("access", "open"),
            "live_state": live_state,
        }

    # ── request routing ───────────────────────────────────────────────────────

    def handle_request(self, req: dict) -> dict:
        """
        Route a high-level request to the guardian's kind-appropriate skill.

        Full call chain:
            other agent
            → VirtualSmartObject.handle_request()
            → GuardianAgent skill method   (intelligence)
            → DumbRelay.handle_op()        (primitive execution)
            → raw device                   (no brain)
            → result back up the chain

        The caller never knows the device is dumb.

        For input_event, requests are refused with consent_required unless
        consent was granted when the relay was created.
        """
        action = req.get("action", "")
        kind   = self._kind

        # ── input_event consent check (before dispatching anything) ────────────
        if kind == KIND_INPUT_EVENT:
            if self.relay_record.get("access") == "consent_required":
                return {
                    "error":   "consent_required",
                    "message": (
                        "Input-device requests require explicit user consent. "
                        "Create the DumbRelay with consent_granted=True."
                    ),
                }

        # ── block_fs actions ──────────────────────────────────────────────────
        if kind == KIND_BLOCK_FS:
            if action == "index":
                return self.guardian.index()
            if action == "search":
                return self.guardian.search(req.get("query", ""))
            if action == "backup":
                target = req.get("target_relay")
                if target is None:
                    return {"error": "backup requires 'target_relay' in request"}
                return self.guardian.backup(target)
            if action == "organize":
                return self.guardian.organize()

        # ── char_stream actions ───────────────────────────────────────────────
        elif kind == KIND_CHAR_STREAM:
            if action == "collect":
                return self.guardian.collect(float(req.get("duration", 2.0)))
            if action == "parse":
                return self.guardian.parse(req.get("pattern", "nmea"))
            if action == "tail":
                return self.guardian.tail()

        # ── input_event actions ───────────────────────────────────────────────
        elif kind == KIND_INPUT_EVENT:
            if action == "decode_events":
                return self.guardian.decode_events()

        # ── sensor_file actions ───────────────────────────────────────────────
        elif kind == KIND_SENSOR_FILE:
            if action == "monitor":
                return self.guardian.monitor(
                    intervals=int(req.get("intervals", 5)),
                    delay=float(req.get("delay", 0.1)),
                )
            if action == "verdict":
                return self.guardian.verdict(
                    warn_threshold=float(req.get("warn_threshold", 75.0)),
                    danger_threshold=float(req.get("danger_threshold", 90.0)),
                )

        # ── raw_generic actions ───────────────────────────────────────────────
        elif kind == KIND_RAW_GENERIC:
            if action == "hexdump":
                return self.guardian.hexdump(int(req.get("length", 64)))
            if action == "capture":
                return self.guardian.capture(int(req.get("length", 256)))

        # ── unknown action fallback ───────────────────────────────────────────
        valid = {
            KIND_BLOCK_FS:    ["index", "search", "backup", "organize"],
            KIND_CHAR_STREAM: ["collect", "parse", "tail"],
            KIND_INPUT_EVENT: ["decode_events"],
            KIND_SENSOR_FILE: ["monitor", "verdict"],
            KIND_RAW_GENERIC: ["hexdump", "capture"],
        }
        return {
            "error":             f"unknown_action:{action}",
            "kind":              kind,
            "available_actions": valid.get(kind, []),
        }
