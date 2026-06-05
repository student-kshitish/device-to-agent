"""
d2a/guardian/virtual_object.py — VirtualSmartObject: the fusion of dumb relay + guardian brain.

KEY PRINCIPLE: The relay is a dumb nerve ending — it only drives the physical
device and relays raw bytes. ALL intelligence (indexing, search, backup,
encryption, organization) lives in the guardian agent, OUTSIDE the hardware.
dumb hardware + borrowed brain = virtual smart object. D2A is the nerve between them.

From the network's perspective this object IS a smart device.  Under the hood:
  other agent → VirtualSmartObject → GuardianAgent skill → DumbRelay primitive → raw device
"""


class VirtualSmartObject:
    """
    The fusion of (dumb relay capability) + (guardian agent skills) presented
    as ONE capability that other agents discover and use.

    To the rest of the D2A network this appears as an intelligent device.
    The underlying hardware is completely brainless — the guardian supplies
    all intelligence from outside the hardware boundary.
    """

    def __init__(self, relay_record: dict, guardian):
        """
        relay_record: a capability dict from DumbRelay.capabilities()[i].
        guardian: a GuardianAgent already attached to the relay.
        """
        self.relay_record = relay_record
        self.guardian     = guardian

    # ── network-facing advertisement ──────────────────────────────────────────

    def advertised_capability(self) -> dict:
        """
        Present as a SMART capability.  Tags like 'indexed' and 'searchable'
        are earned by the guardian's skills, not by the device hardware.
        'live_state' reflects current index depth and available space.
        """
        free_bytes = self.relay_record.get("free_bytes", 0)

        relay = self.relay_record.get("relay_ref")
        if relay:
            caps = relay.capabilities()
            if caps:
                free_bytes = caps[0].get("free_bytes", 0)

        return {
            "name":      "smart_storage",
            "tags":      ["indexed", "searchable", "backup_capable",
                          "organized", "virtual_smart_object"],
            "backed_by": self.relay_record.get("relay_node_id"),
            "guardian":  self.guardian.agent_id,
            "skills":    list(self.guardian.skills),
            "live_state": {
                "entries_indexed": len(self.guardian._index),
                "free_bytes":      free_bytes,
            },
        }

    # ── request routing ───────────────────────────────────────────────────────

    def handle_request(self, req: dict) -> dict:
        """
        Route a high-level request to the guardian's skill methods, which in
        turn use relay primitives to reach the raw device.

        Full call chain:
            other agent
            → VirtualSmartObject.handle_request()
            → GuardianAgent skill method  (intelligence here)
            → DumbRelay.handle_op()       (primitive execution)
            → raw device                  (no brain here)
            → result back up the chain

        The caller never knows the device is dumb.
        """
        action = req.get("action", "")

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

        return {
            "error":             f"unknown_action:{action}",
            "available_actions": ["index", "search", "backup", "organize"],
        }
