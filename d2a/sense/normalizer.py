"""
d2a/sense/normalizer.py — Normalizer: raw device-specific signals → 0..1.

All numeric signals are mapped to the [0, 1] interval using named, documented
constants so thresholds are easy to adjust without hunting through logic.

Consistent ACROSS devices: a phone and a GPU workstation produce the same
normalized shape so agents never need to know the underlying hardware.

Rules:
  - Percentages (cpu%, util%, battery%, mem%)  → value / 100
  - Temperatures                                → (val - TEMP_SAFE_MIN) / range, clamped
  - Load averages                               → load / cpu_count, clamped
  - IO rates                                    → rate / realistic_ceiling, clamped
  - Non-numeric / metadata / capacity fields    → pass through unchanged
  - Unavailable sentinels                       → pass through as-is
"""

# ── Temperature safe operating range (°C) ─────────────────────────────────────
# TEMP_SAFE_MIN: room-temperature idle baseline; below this maps to 0.
# TEMP_SAFE_MAX: near-shutdown limit for most laptop/desktop CPUs and GPUs.
#   Above this limit most silicon throttles heavily or triggers BIOS shutdown.
#   Clamp to 1.0 rather than extrapolate — values above 90°C are all "critical".
TEMP_SAFE_MIN: float = 20.0   # °C
TEMP_SAFE_MAX: float = 90.0   # °C

# ── Load normalization fallback ────────────────────────────────────────────────
# load1/5/15 are divided by cpu_count (present in the same cpu source dict).
# If cpu_count is absent, this default prevents division by zero.
LOAD_SCALE_DEFAULT: int = 8   # assumed core count when cpu_count is unavailable

# ── Disk I/O rate ceilings (KB/s and IOPS) ────────────────────────────────────
# Values above these are clamped to 1.0 — they mean "at the device's practical max".
# Chosen to represent a fast consumer NVMe SSD.
DISK_IO_MAX_KB_S: float  = 500_000.0  # 500 MB/s — NVMe sequential ceiling
DISK_IOPS_MAX:   float   =  50_000.0  # 50 k IOPS — NVMe random ceiling

# ── Network I/O rate ceilings ──────────────────────────────────────────────────
# Chosen to represent a 1 Gbit/s NIC (common consumer maximum).
NET_IO_MAX_KB_S: float   = 125_000.0  # ~1 Gbit/s = 125 MB/s
NET_PPS_MAX:     float   = 100_000.0  # pps ceiling for 1 Gbit/s link


def _clamp(val: float) -> float:
    return max(0.0, min(1.0, val))


def _norm_pct(val: float) -> float:
    """Percentage (0..100) → [0, 1]."""
    return _clamp(val / 100.0)


def _norm_temp(val: float) -> float:
    """Temperature in °C → [0, 1] over the safe operating range."""
    span = TEMP_SAFE_MAX - TEMP_SAFE_MIN
    if span == 0:
        return 0.0
    return _clamp((val - TEMP_SAFE_MIN) / span)


def _norm_rate(val: float, cap: float) -> float:
    """Rate value → [0, 1] relative to a ceiling."""
    if cap <= 0:
        return 0.0
    return _clamp(val / cap)


class Normalizer:
    """
    Maps {source_name: raw_dict} → {source_name: normalized_dict}.
    Preserves key structure so downstream consumers always know what they see.
    """

    def normalize(self, raw: dict) -> dict:
        """Scale all numeric signals to [0, 1]. Unavailable sources pass through."""
        result: dict = {}
        for source_name, source_data in raw.items():
            if not isinstance(source_data, dict):
                result[source_name] = source_data
                continue
            if source_data.get("_unavailable"):
                result[source_name] = {"_unavailable": True}
                continue
            result[source_name] = self._normalize_source(source_name, source_data)
        return result

    def _normalize_source(self, source_name: str, data: dict) -> dict:
        dispatch = {
            "cpu":     self._norm_cpu,
            "memory":  self._norm_memory,
            "thermal": self._norm_thermal,
            "battery": self._norm_battery,
            "gpu":     self._norm_gpu,
            "disk_io": self._norm_disk_io,
            "net_io":  self._norm_net_io,
        }
        handler = dispatch.get(source_name)
        return handler(data) if handler else self._norm_generic(data)

    # ── per-source normalizers ─────────────────────────────────────────────────

    def _norm_cpu(self, d: dict) -> dict:
        cpu_count = float(d.get("cpu_count") or LOAD_SCALE_DEFAULT)
        out: dict = {}
        for k, v in d.items():
            if k in ("load1", "load5", "load15"):
                out[k] = _clamp(float(v) / cpu_count) if isinstance(v, (int, float)) else v
            elif k == "util_pct":
                out[k] = _norm_pct(float(v)) if isinstance(v, (int, float)) else v
            elif k == "util_by_core":
                if isinstance(v, dict):
                    out[k] = {ck: _norm_pct(float(cv))
                               for ck, cv in v.items() if isinstance(cv, (int, float))}
                else:
                    out[k] = v
            else:
                out[k] = v  # cpu_count, arch → pass through (metadata, not utilization)
        return out

    def _norm_memory(self, d: dict) -> dict:
        total_mb = float(d.get("total_mb") or 1)
        out: dict = {}
        for k, v in d.items():
            if k == "used_percent":
                out[k] = _norm_pct(float(v)) if isinstance(v, (int, float)) else v
            elif k in ("available_mb", "free_mb", "cached_mb"):
                # express as fraction of total RAM so phones and servers compare cleanly
                out[k] = _clamp(float(v) / total_mb) if isinstance(v, (int, float)) else v
            else:
                out[k] = v  # total_mb → pass through (absolute capacity, not utilization)
        return out

    def _norm_thermal(self, d: dict) -> dict:
        out: dict = {}
        for k, v in d.items():
            if k in ("max_temp_c", "min_temp_c"):
                out[k] = _norm_temp(float(v)) if isinstance(v, (int, float)) else v
            elif k == "temps_c":
                out[k] = [_norm_temp(float(t)) for t in v if isinstance(t, (int, float))]
            elif k == "zones":
                out[k] = [
                    {zk: (_norm_temp(float(zv)) if zk == "temp_c"
                                                    and isinstance(zv, (int, float)) else zv)
                     for zk, zv in zone.items()}
                    for zone in v
                ] if isinstance(v, list) else v
            else:
                out[k] = v  # zone_count → pass through
        return out

    def _norm_battery(self, d: dict) -> dict:
        out: dict = {}
        for k, v in d.items():
            if k == "capacity_pct":
                out[k] = _norm_pct(float(v)) if isinstance(v, (int, float)) else v
            else:
                out[k] = v  # energy_now, status, path → pass through raw
        return out

    def _norm_gpu(self, d: dict) -> dict:
        vram_total = float(d.get("vram_total_mib") or 1)
        out: dict = {}
        for k, v in d.items():
            if k == "util_pct":
                out[k] = _norm_pct(float(v)) if isinstance(v, (int, float)) else v
            elif k == "vram_used_mib":
                out[k] = _clamp(float(v) / vram_total) if isinstance(v, (int, float)) else v
            else:
                out[k] = v  # vram_total_mib, vendor, name → pass through
        return out

    def _norm_disk_io(self, d: dict) -> dict:
        """Aggregate all device rates into a single normalized per-device-class value."""
        devices = d.get("devices", {})
        if not isinstance(devices, dict) or not devices:
            # First call before a diff sample exists — no penalty, no data yet.
            return {"_no_io_data": True}
        total_read_kb   = sum(dev.get("read_kb_s",   0) for dev in devices.values())
        total_write_kb  = sum(dev.get("write_kb_s",  0) for dev in devices.values())
        total_read_iops = sum(dev.get("read_iops",   0) for dev in devices.values())
        total_write_iops= sum(dev.get("write_iops",  0) for dev in devices.values())
        return {
            "read_kb_s":   _norm_rate(total_read_kb,    DISK_IO_MAX_KB_S),
            "write_kb_s":  _norm_rate(total_write_kb,   DISK_IO_MAX_KB_S),
            "read_iops":   _norm_rate(total_read_iops,  DISK_IOPS_MAX),
            "write_iops":  _norm_rate(total_write_iops, DISK_IOPS_MAX),
        }

    def _norm_net_io(self, d: dict) -> dict:
        """Aggregate all interface rates into a single normalized value."""
        interfaces = d.get("interfaces", {})
        if not isinstance(interfaces, dict) or not interfaces:
            return {"_no_io_data": True}
        total_rx     = sum(iface.get("rx_kb_s", 0) for iface in interfaces.values())
        total_tx     = sum(iface.get("tx_kb_s", 0) for iface in interfaces.values())
        total_rx_pps = sum(iface.get("rx_pps",  0) for iface in interfaces.values())
        total_tx_pps = sum(iface.get("tx_pps",  0) for iface in interfaces.values())
        return {
            "rx_kb_s": _norm_rate(total_rx,     NET_IO_MAX_KB_S),
            "tx_kb_s": _norm_rate(total_tx,     NET_IO_MAX_KB_S),
            "rx_pps":  _norm_rate(total_rx_pps, NET_PPS_MAX),
            "tx_pps":  _norm_rate(total_tx_pps, NET_PPS_MAX),
        }

    def _norm_generic(self, d: dict) -> dict:
        """Fallback for unknown sources: pattern-match field names."""
        _pct_suffixes = ("_pct", "_percent", "_util")
        out: dict = {}
        for k, v in d.items():
            if isinstance(v, (int, float)) and any(k.endswith(s) for s in _pct_suffixes):
                out[k] = _norm_pct(float(v))
            else:
                out[k] = v
        return out
