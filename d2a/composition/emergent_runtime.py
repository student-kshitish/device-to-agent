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

    # ── merged_stream ─────────────────────────────────────────────────────────

    def read_merged(self, max_per_member: int = 64, timeout: float = 0.2) -> dict:
        """
        Read from all char_stream members in round-robin order.
        Returns {"chunks": [{member_index, node_id, data_hex}, ...], "members": N}.

        Each member is called with read_stream; empty/error chunks are skipped.
        Members whose relay is unavailable are skipped silently (graceful degradation).
        """
        if self._kind != "merged_stream":
            return {"error": "read_merged() only valid for merged_stream handle"}

        chunks: list[dict] = []
        for i, slot in sorted(self._placement.items()):
            node_id = slot.get("node_id")
            relay   = self._relay_map.get(node_id)
            if relay is None:
                continue
            # Ensure the stream is open (idempotent: already-open returns an error we ignore)
            relay.handle_op({"op": "open_stream"})
            # No 'path' field: relay uses its own registered root (exact-path jail)
            result = relay.handle_op({
                "op":      "read_stream",
                "max_bytes": max_per_member,
                "timeout":   timeout,
            })
            # relay returns {"data": hex_str, ...} where "data" is the raw bytes as hex
            raw_hex = result.get("data") or result.get("data_hex", "")
            if "error" not in result and raw_hex:
                chunks.append({
                    "member_index": i,
                    "node_id":      node_id,
                    "data_hex":     raw_hex,
                })
        return {"chunks": chunks, "members": len(self._placement)}

    def tail_all(self, lines: int = 10) -> dict:
        """
        Read the last `lines` text lines from each char_stream member.
        Returns {"tails": {member_index: [str, ...]}}.
        """
        if self._kind != "merged_stream":
            return {"error": "tail_all() only valid for merged_stream handle"}

        tails: dict[int, list[str]] = {}
        for i, slot in sorted(self._placement.items()):
            node_id = slot.get("node_id")
            relay   = self._relay_map.get(node_id)
            if relay is None:
                tails[i] = []
                continue
            # Ensure stream is open; collect block and split into lines
            relay.handle_op({"op": "open_stream"})
            result = relay.handle_op({
                "op":       "read_stream",
                "max_bytes": lines * 128,
                "timeout":   0.1,
            })
            raw_hex = result.get("data") or result.get("data_hex", "")
            if "error" in result or not raw_hex:
                tails[i] = []
            else:
                raw   = bytes.fromhex(raw_hex).decode("utf-8", errors="replace")
                tails[i] = raw.splitlines()[-lines:]
        return {"tails": tails}

    # ── sensor_array ──────────────────────────────────────────────────────────

    def read_all(self) -> dict:
        """
        Read the current scalar value from each sensor_file member.
        Returns {
            "readings": {member_index: {"node_id": str, "value": str, "raw": str}},
            "aggregate": {"min": float, "max": float, "mean": float, "count": int},
        }.
        Numeric conversion is best-effort; non-numeric values are included in
        readings but excluded from the aggregate (not silently dropped).
        """
        if self._kind != "sensor_array":
            return {"error": "read_all() only valid for sensor_array handle"}

        readings: dict[int, dict] = {}
        numeric:  list[float]     = []

        for i, slot in sorted(self._placement.items()):
            node_id = slot.get("node_id")
            relay   = self._relay_map.get(node_id)
            if relay is None:
                readings[i] = {"node_id": node_id, "error": "relay_unavailable"}
                continue
            # No 'path' field: sensor_file relay reads its own registered path
            result = relay.handle_op({"op": "read_value"})
            raw = result.get("value", "")
            rec = {"node_id": node_id, "raw": raw}
            try:
                val = float(raw.strip())
                rec["value"] = val
                numeric.append(val)
            except (ValueError, AttributeError):
                rec["value"] = raw
            readings[i] = rec

        agg: dict = {"count": len(numeric)}
        if numeric:
            agg["min"]  = min(numeric)
            agg["max"]  = max(numeric)
            agg["mean"] = round(sum(numeric) / len(numeric), 6)

        return {"readings": readings, "aggregate": agg}

    def verdict_all(self, warn: float, danger: float) -> dict:
        """
        Read all sensor members and classify: ok / warn / danger.
        Returns {"verdicts": {member_index: "ok"|"warn"|"danger"|"error"},
                 "summary": "ok"|"warn"|"danger"}.
        Overall summary = worst level seen.
        """
        if self._kind != "sensor_array":
            return {"error": "verdict_all() only valid for sensor_array handle"}

        all_data = self.read_all()
        if "error" in all_data:
            return all_data

        level_rank = {"ok": 0, "warn": 1, "danger": 2, "error": -1}
        verdicts:  dict[int, str] = {}
        worst      = "ok"

        for idx, rec in all_data["readings"].items():
            if "error" in rec:
                verdicts[idx] = "error"
                continue
            try:
                val = float(rec.get("raw", "").strip())
            except (ValueError, AttributeError):
                verdicts[idx] = "error"
                continue
            if val >= danger:
                lv = "danger"
            elif val >= warn:
                lv = "warn"
            else:
                lv = "ok"
            verdicts[idx] = lv
            if level_rank.get(lv, 0) > level_rank.get(worst, 0):
                worst = lv

        return {"verdicts": verdicts, "summary": worst}

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
        elif self._kind == "merged_stream":
            base.update({
                "member_count": len(self._placement),
                "members": {
                    i: {"node_id": slot.get("node_id")}
                    for i, slot in self._placement.items()
                },
            })
        elif self._kind == "sensor_array":
            base.update({
                "member_count": len(self._placement),
                "members": {
                    i: {"node_id": slot.get("node_id"), "member_id": slot.get("member_id")}
                    for i, slot in self._placement.items()
                },
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
