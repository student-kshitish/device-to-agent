"""
d2a/composition/emergent_runtime.py — EmergentDeviceHandle.

CORE PRINCIPLE: the parts become NEURONS of one body; the BRAIN is always
the agent.  The EmergentDevice itself has NO logic; all routing decisions
come from the synthesizer plan.  The handle just executes those decisions
against the live relay primitives.

    agent (the brain)
      └─ EmergentDeviceHandle (executes the plan)
           ├─ DumbRelay member 0  ─── raw device A
           ├─ DumbRelay member 1  ─── raw device B
           └─ DumbRelay member 2  ─── raw device C
"""

from __future__ import annotations
from d2a.composition.synthesis_types import EmergentDevice, TIERED_FAST_MAX


class EmergentDeviceHandle:
    """
    Unified interface over multiple bound members.

    pooled_storage — write(key, data)/read(key): routes to the right member
        per the fill_sequential placement rule encoded in placement_map.
        total_capacity = sum of member capacities.

    tiered_memory — put(key, val)/get(key): hot data in fast tier (in-memory
        LRU cache up to FAST_MAX_ENTRIES); evicted entries go to slow-tier relay;
        get() checks fast first, then slow.  A simple, legible LRU policy.
    """

    def __init__(
        self,
        emergent_device: EmergentDevice,
        bindings: list[dict],
    ):
        self._device       = emergent_device
        self._bindings     = bindings
        self._kind         = emergent_device.kind
        self._placement    = emergent_device.placement_map  # plan from synthesizer

        # Build node_id → relay lookup from binding dicts and placement_map
        self._relay_map: dict[str, object] = {}
        for b in bindings:
            ref = b.get("relay_ref")
            if ref is not None:
                self._relay_map[b["provider_node_id"]] = ref
        # Also harvest from placement_map (belt-and-suspenders)
        for slot in self._placement.values():
            if isinstance(slot, dict):
                ref = slot.get("relay_ref")
                nid = slot.get("node_id")
                if ref is not None and nid:
                    self._relay_map[nid] = ref

        # Pooled storage state
        self._file_map:       dict[str, int] = {}  # key → member_index
        self._degraded:       set[str]       = set()  # node_ids known unavailable

        # Tiered memory state (FAST tier is in-memory; SLOW tier is a relay)
        self._fast_cache: dict[str, bytes] = {}   # key → bytes (hot items)
        self._fast_order: list[str]        = []   # LRU order: front=oldest, back=newest
        self._fast_max                     = int(
            self._placement.get("fast", {}).get("max_entries", TIERED_FAST_MAX)
        )
        self._slow_relay = None
        if self._kind == "tiered_memory":
            slow_nid        = self._placement.get("slow", {}).get("node_id")
            self._slow_relay = self._relay_map.get(slow_nid)

    # ── pooled_storage ────────────────────────────────────────────────────────

    def write(self, key: str, data: bytes) -> dict:
        """
        Write data to the pooled virtual device.  Routes to the first
        available member per fill_sequential placement (member 0 first, etc.).
        Records the placement so read() can route back correctly.
        """
        if self._kind != "pooled_storage":
            return {"error": "write() only valid for pooled_storage handle"}

        for i in sorted(self._placement.keys()):
            node_id = self._placement[i]["node_id"]
            if node_id in self._degraded:
                continue
            relay = self._relay_map.get(node_id)
            if relay is None:
                continue
            w = relay.handle_op({
                "op": "write_bytes", "path": key,
                "offset": 0, "data": data.hex(),
            })
            if "error" not in w:
                self._file_map[key] = i
                return {
                    "ok":              True,
                    "placed_on":       i,
                    "node_id":         node_id,
                    "bytes_written":   w.get("bytes_written", len(data)),
                }
        return {"error": "no available member for write"}

    def read(self, key: str) -> dict:
        """
        Read data from the pooled virtual device using the stored placement map.
        Routes to the member that holds the key.
        """
        if self._kind != "pooled_storage":
            return {"error": "read() only valid for pooled_storage handle"}

        idx = self._file_map.get(key)
        if idx is None:
            return {"error": "key_not_found", "key": key}

        slot    = self._placement.get(idx, {})
        node_id = slot.get("node_id")
        relay   = self._relay_map.get(node_id)
        if relay is None or node_id in self._degraded:
            return {"error": "member_unavailable", "member": idx, "node_id": node_id}

        stat = relay.handle_op({"op": "stat", "path": key})
        if "error" in stat:
            return {"error": "stat_failed", "detail": stat}
        size = stat.get("size", 0)

        r = relay.handle_op({
            "op": "read_bytes", "path": key,
            "offset": 0, "length": max(size, 1),
        })
        if "error" in r:
            return r
        return {
            "data":        bytes.fromhex(r["data"]),
            "from_member": idx,
            "node_id":     node_id,
        }

    # ── tiered_memory ─────────────────────────────────────────────────────────

    def put(self, key: str, value: bytes) -> dict:
        """
        Put into fast tier (in-memory LRU).  If fast tier is full, evict the
        coldest entry to the slow-tier relay.  No ML — simple LRU counting.
        """
        if self._kind != "tiered_memory":
            return {"error": "put() only valid for tiered_memory handle"}

        # Remove stale fast-tier entry if updating an existing key
        if key in self._fast_cache:
            self._fast_order.remove(key)

        # Evict LRU entry to slow relay if at capacity
        if len(self._fast_cache) >= self._fast_max:
            evict_key = self._fast_order.pop(0)      # oldest = front
            evict_val = self._fast_cache.pop(evict_key)
            if self._slow_relay is not None:
                self._slow_relay.handle_op({
                    "op": "write_bytes", "path": evict_key,
                    "offset": 0, "data": evict_val.hex(),
                })

        self._fast_cache[key] = value
        self._fast_order.append(key)                  # newest = back (MRU)
        return {
            "ok":         True,
            "tier":       "fast",
            "fast_count": len(self._fast_cache),
        }

    def get(self, key: str) -> dict:
        """
        Get from fast tier first, then slow-tier relay.
        Returns {"data": bytes, "tier": "fast"|"slow"} or {"error": ...}.
        """
        if self._kind != "tiered_memory":
            return {"error": "get() only valid for tiered_memory handle"}

        if key in self._fast_cache:
            return {"data": self._fast_cache[key], "tier": "fast"}

        if self._slow_relay is None:
            return {"error": "slow_relay_unavailable"}

        stat = self._slow_relay.handle_op({"op": "stat", "path": key})
        if "error" in stat:
            return {"error": "key_not_found", "key": key}
        size = stat.get("size", 0)

        r = self._slow_relay.handle_op({
            "op": "read_bytes", "path": key,
            "offset": 0, "length": max(size, 1),
        })
        if "error" in r:
            return r
        return {"data": bytes.fromhex(r["data"]), "tier": "slow"}

    # ── stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        base = {
            "kind":             self._kind,
            "degraded_members": list(self._degraded),
        }
        if self._kind == "pooled_storage":
            total = self._device.combined_contract.get("total_bytes", 0)
            base.update({
                "total_bytes":  total,
                "member_count": len(self._placement),
                "files_stored": len(self._file_map),
                "placement":    dict(self._file_map),
                "per_member": {
                    i: {
                        "node_id":    slot["node_id"],
                        "byte_range": slot.get("byte_range"),
                        "files":      [k for k, v in self._file_map.items() if v == i],
                    }
                    for i, slot in self._placement.items()
                },
            })
        elif self._kind == "tiered_memory":
            base.update({
                "fast_entries": len(self._fast_cache),
                "fast_max":     self._fast_max,
                "fast_keys":    list(self._fast_cache.keys()),
                "fast_order":   list(self._fast_order),
            })
        return base

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_composition(cls, composition) -> "EmergentDeviceHandle":
        """Build a handle from a just-bound Composition (synthesis goal)."""
        meta     = composition.bound_blueprint.synthesis_metadata or {}
        emergent = meta.get("emergent_device")
        if emergent is None:
            raise ValueError("bound blueprint has no synthesis_metadata.emergent_device")
        return cls(emergent, composition.bindings)
