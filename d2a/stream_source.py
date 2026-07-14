"""
d2a/stream_source.py — fresh kernel signal sources.

Each source does a FRESH read on every call. No caching, no background loops.
Missing files/commands return None (never raise). Warnings logged once per source.
"""

import glob
import os
import shutil
import subprocess
import time

_warned: set = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[stream_source] {msg}")


class SignalSource:
    name: str = "base"

    def read(self) -> dict | None:
        raise NotImplementedError


# ── CPU + load ─────────────────────────────────────────────────────────────────

class CPUSource(SignalSource):
    """
    Reads /proc/stat (per-core utilization via jiffies diff) and /proc/loadavg.
    Keeps only the last sample to compute the delta; computed lazily on read().
    First call returns load/cpu_count but no util_pct (no previous sample to diff).
    """

    name = "cpu"

    def __init__(self) -> None:
        # last sample: dict[core_name, (total_jiffies, idle_jiffies)] and ts
        self._last: tuple[dict, float] | None = None

    def read(self) -> dict | None:
        try:
            result: dict = {}

            if os.path.exists("/proc/loadavg"):
                try:
                    parts = open("/proc/loadavg").read().split()
                    result["load1"]  = float(parts[0])
                    result["load5"]  = float(parts[1])
                    result["load15"] = float(parts[2])
                except Exception:
                    pass

            result["cpu_count"] = os.cpu_count()

            if os.path.exists("/proc/stat"):
                try:
                    now = time.time()
                    current: dict = {}
                    for line in open("/proc/stat"):
                        if not line.startswith("cpu"):
                            continue
                        parts = line.split()
                        core = parts[0]
                        vals = [int(x) for x in parts[1:]]
                        total = sum(vals)
                        # idle + iowait
                        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                        current[core] = (total, idle)

                    if self._last is not None:
                        last_cores, _ = self._last
                        util_by_core: dict = {}
                        for core, (total, idle) in current.items():
                            if core in last_cores:
                                lt, li = last_cores[core]
                                dtotal = total - lt
                                didle  = idle  - li
                                if dtotal > 0:
                                    util_by_core[core] = round((1.0 - didle / dtotal) * 100.0, 1)
                                else:
                                    util_by_core[core] = 0.0
                        if util_by_core:
                            result["util_by_core"] = util_by_core
                            non_agg = {k: v for k, v in util_by_core.items() if k != "cpu"}
                            if non_agg:
                                result["util_pct"] = round(sum(non_agg.values()) / len(non_agg), 1)
                            elif "cpu" in util_by_core:
                                result["util_pct"] = util_by_core["cpu"]

                    self._last = (current, now)
                except Exception:
                    pass

            return result or None
        except Exception:
            _warn_once("cpu", "CPUSource: read failed")
            return None


# ── Memory ─────────────────────────────────────────────────────────────────────

class MemorySource(SignalSource):
    name = "memory"

    def read(self) -> dict | None:
        try:
            if not os.path.exists("/proc/meminfo"):
                return None
            data: dict = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        data[parts[0].strip()] = int(parts[1].split()[0])
            total_kb = data.get("MemTotal", 0)
            avail_kb = data.get("MemAvailable", 0)
            return {
                "total_mb":     round(total_kb / 1024, 1),
                "available_mb": round(avail_kb / 1024, 1),
                "free_mb":      round(data.get("MemFree", 0) / 1024, 1),
                "cached_mb":    round(data.get("Cached", 0) / 1024, 1),
                "used_percent": round((total_kb - avail_kb) / total_kb * 100, 1) if total_kb else 0,
            }
        except Exception:
            _warn_once("memory", "MemorySource: read failed")
            return None


# ── GPU ────────────────────────────────────────────────────────────────────────

class GPUSource(SignalSource):
    """
    Reads GPU utilization + VRAM via nvidia-smi, rocm-smi, AMD sysfs, or generic DRM.
    Each call is a fresh subprocess/sysfs read; timeout=5s.
    """

    name = "gpu"

    def read(self) -> dict | None:
        # nvidia-smi
        try:
            if shutil.which("nvidia-smi"):
                r = subprocess.run(
                    ["nvidia-smi",
                     "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    parts = [p.strip() for p in r.stdout.strip().split(",")]
                    return {
                        "vendor":          "nvidia",
                        "name":            parts[0],
                        "vram_total_mib":  int(parts[1]),
                        "vram_used_mib":   int(parts[2]),
                        "util_pct":        int(parts[3]),
                    }
        except Exception:
            pass

        # rocm-smi (AMD)
        try:
            if shutil.which("rocm-smi"):
                r = subprocess.run(
                    ["rocm-smi", "--showmeminfo", "vram", "--json"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    return {"vendor": "amd", "raw": r.stdout.strip()[:200]}
        except Exception:
            pass

        # AMD sysfs
        try:
            for p in glob.glob("/sys/class/drm/card*/device/vendor"):
                if open(p).read().strip() == "0x1002":
                    util_path = os.path.join(os.path.dirname(p), "gpu_busy_percent")
                    util = int(open(util_path).read().strip()) if os.path.exists(util_path) else None
                    return {"vendor": "amd", "present": True, "util_pct": util}
        except Exception:
            pass

        # generic DRM
        try:
            cards = glob.glob("/sys/class/drm/card*/device")
            if cards:
                return {"vendor": "unknown", "present": True, "card_count": len(cards)}
        except Exception:
            pass

        return None


# ── Thermal ────────────────────────────────────────────────────────────────────

class ThermalSource(SignalSource):
    name = "thermal"

    def read(self) -> dict | None:
        try:
            paths = sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))
            if not paths:
                return None
            temps:  list[float] = []
            zones:  list[dict]  = []
            for p in paths:
                try:
                    val = round(int(open(p).read().strip()) / 1000, 1)
                    temps.append(val)
                    type_p    = os.path.join(os.path.dirname(p), "type")
                    zone_type = open(type_p).read().strip() if os.path.exists(type_p) else "unknown"
                    zones.append({"type": zone_type, "temp_c": val})
                except Exception:
                    pass
            if not temps:
                return None
            return {
                "temps_c":    temps,
                "zones":      zones,
                "zone_count": len(temps),
                "max_temp_c": max(temps),
                "min_temp_c": min(temps),
            }
        except Exception:
            _warn_once("thermal", "ThermalSource: read failed")
            return None


# ── Battery ────────────────────────────────────────────────────────────────────

class BatterySource(SignalSource):
    name = "battery"

    def read(self) -> dict | None:
        try:
            bats = glob.glob("/sys/class/power_supply/BAT*")
            if not bats:
                return None
            bat = bats[0]
            cap_path  = os.path.join(bat, "capacity")
            stat_path = os.path.join(bat, "status")
            if not os.path.exists(cap_path):
                return None
            result = {
                "capacity_pct": int(open(cap_path).read().strip()),
                "status":       open(stat_path).read().strip() if os.path.exists(stat_path) else "unknown",
                "path":         os.path.basename(bat),
            }
            for fname in ("energy_now", "energy_full", "charge_now", "charge_full", "voltage_now"):
                fpath = os.path.join(bat, fname)
                if os.path.exists(fpath):
                    try:
                        result[fname] = int(open(fpath).read().strip())
                    except Exception:
                        pass
            return result
        except Exception:
            _warn_once("battery", "BatterySource: read failed")
            return None


# ── Disk I/O ───────────────────────────────────────────────────────────────────

class DiskIOSource(SignalSource):
    """
    Reads /proc/diskstats. First call stores the sample; subsequent calls
    compute rates (IOPS, KB/s) by differencing against the previous sample.
    """

    name = "disk_io"

    def __init__(self) -> None:
        # (stats_by_dev, ts): stats_by_dev[dev] = (reads, read_sectors, writes, write_sectors)
        self._last: tuple[dict, float] | None = None

    def read(self) -> dict | None:
        try:
            if not os.path.exists("/proc/diskstats"):
                return None
            now = time.time()
            current: dict = {}
            with open("/proc/diskstats") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 14:
                        continue
                    dev = parts[2]
                    reads        = int(parts[3])
                    read_sectors = int(parts[5])
                    writes       = int(parts[7])
                    write_sectors = int(parts[9])
                    current[dev] = (reads, read_sectors, writes, write_sectors)

            devices: dict = {}
            if self._last is not None:
                last_devs, last_ts = self._last
                dt = now - last_ts
                if dt > 0:
                    for dev, (rd, rs, wr, ws) in current.items():
                        if dev not in last_devs:
                            continue
                        lr, lrs, lw, lws = last_devs[dev]
                        devices[dev] = {
                            "read_iops":  round((rd - lr)  / dt, 1),
                            "write_iops": round((wr - lw)  / dt, 1),
                            "read_kb_s":  round((rs - lrs) * 512 / 1024 / dt, 1),
                            "write_kb_s": round((ws - lws) * 512 / 1024 / dt, 1),
                        }

            self._last = (current, now)
            return {"devices": devices, "ts": now} if (devices or self._last) else None
        except Exception:
            _warn_once("disk_io", "DiskIOSource: read failed")
            return None


# ── Network I/O ────────────────────────────────────────────────────────────────

class NetIOSource(SignalSource):
    """
    Reads /proc/net/dev. First call stores the sample; subsequent calls
    compute rates (KB/s, pps) by differencing against the previous sample.
    Skips loopback (lo).
    """

    name = "net_io"

    def __init__(self) -> None:
        # (stats_by_iface, ts): stats[iface] = (rx_bytes, rx_pkts, tx_bytes, tx_pkts)
        self._last: tuple[dict, float] | None = None

    def read(self) -> dict | None:
        try:
            if not os.path.exists("/proc/net/dev"):
                return None
            now = time.time()
            current: dict = {}
            with open("/proc/net/dev") as f:
                for line in f:
                    line = line.strip()
                    if ":" not in line:
                        continue
                    iface, rest = line.split(":", 1)
                    iface = iface.strip()
                    parts = rest.split()
                    if len(parts) < 10:
                        continue
                    current[iface] = (int(parts[0]), int(parts[1]),
                                      int(parts[8]), int(parts[9]))

            interfaces: dict = {}
            if self._last is not None:
                last_ifaces, last_ts = self._last
                dt = now - last_ts
                if dt > 0:
                    for iface, (rxb, rxp, txb, txp) in current.items():
                        if iface == "lo" or iface not in last_ifaces:
                            continue
                        lrxb, lrxp, ltxb, ltxp = last_ifaces[iface]
                        interfaces[iface] = {
                            "rx_kb_s": round((rxb - lrxb) / 1024 / dt, 2),
                            "tx_kb_s": round((txb - ltxb) / 1024 / dt, 2),
                            "rx_pps":  round((rxp - lrxp) / dt, 1),
                            "tx_pps":  round((txp - ltxp) / dt, 1),
                        }

            self._last = (current, now)
            return {"interfaces": interfaces, "ts": now} if (interfaces or self._last) else None
        except Exception:
            _warn_once("net_io", "NetIOSource: read failed")
            return None


# ── Privacy-safe metadata sources (for consent-gated resources) ───────────────
# These sources NEVER capture actual frames / audio / location coordinates.
# They return device-availability metadata only, with captured=False as a
# machine-readable privacy guarantee.
# Real capture is a future, consent-gated module; these placeholders are safe.

class CameraMetaSource(SignalSource):
    """Presence metadata only. Does NOT open the camera or capture any frame."""
    name = "camera"

    def read(self) -> dict | None:
        nodes = glob.glob("/dev/video*")
        return {
            "available":  len(nodes) > 0,
            "node_count": len(nodes),
            "nodes":      sorted(nodes)[:4],
            "captured":   False,   # capture gated behind explicit consent — not implemented
        }


class MicrophoneMetaSource(SignalSource):
    """Presence metadata only. Does NOT record or open any audio stream."""
    name = "microphone"

    def read(self) -> dict | None:
        capture_nodes = glob.glob("/dev/snd/pcmC*D*c")
        has_cards     = False
        if os.path.exists("/proc/asound/cards"):
            txt       = open("/proc/asound/cards").read().strip()
            has_cards = bool(txt) and "no soundcards" not in txt.lower()
        return {
            "present":       has_cards or bool(capture_nodes),
            "capture_nodes": len(capture_nodes),
            "captured":      False,   # audio recording gated behind explicit consent
        }


class LocationMetaSource(SignalSource):
    """Presence metadata only. Does NOT read GPS or compute any coordinates."""
    name = "location"

    def read(self) -> dict | None:
        candidates = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        return {
            "present":           bool(candidates),
            "source":            "gps_serial_candidate" if candidates else "none_confirmed",
            "coords_captured":   False,   # geolocation gated behind explicit consent
        }


class DisplayMetaSource(SignalSource):
    """Presence metadata only. Does NOT capture screenshots."""
    name = "display"

    def read(self) -> dict | None:
        statuses: dict = {}
        for p in sorted(glob.glob("/sys/class/drm/*/status")):
            name = os.path.basename(os.path.dirname(p))
            try:
                statuses[name] = open(p).read().strip()
            except Exception:
                pass
        connected = sum(1 for s in statuses.values() if s == "connected")
        return {
            "connected_outputs":    connected,
            "total_outputs":        len(statuses),
            "screenshot_captured":  False,   # screenshot gated behind explicit consent
        }


class StorageSource(SignalSource):
    """Disk usage stats for root and mounted volumes. No file content read."""
    name = "storage"

    def read(self) -> dict | None:
        try:
            root   = shutil.disk_usage("/")
            mounts = [{
                "path":     "/",
                "total_gb": round(root.total / 1e9, 1),
                "free_gb":  round(root.free  / 1e9, 1),
                "used_pct": round(root.used  / root.total * 100, 1),
            }]
            for base in ("/mnt", "/media"):
                if not os.path.isdir(base):
                    continue
                try:
                    for entry in os.listdir(base):
                        p = os.path.join(base, entry)
                        try:
                            u = shutil.disk_usage(p)
                            if u.total > 0:
                                mounts.append({
                                    "path":     p,
                                    "total_gb": round(u.total / 1e9, 1),
                                    "free_gb":  round(u.free  / 1e9, 1),
                                    "used_pct": round(u.used  / u.total * 100, 1),
                                })
                        except Exception:
                            pass
                except Exception:
                    pass
            return {
                "total_gb": round(root.total / 1e9, 1),
                "free_gb":  round(root.free  / 1e9, 1),
                "mounts":   mounts,
            }
        except Exception:
            _warn_once("storage", "StorageSource: read failed")
            return None


class NetworkMetaSource(SignalSource):
    """Interface list + operational state. Does NOT expose traffic contents."""
    name = "network"

    def read(self) -> dict | None:
        try:
            ifaces = []
            for p in sorted(glob.glob("/sys/class/net/*")):
                name = os.path.basename(p)
                if name == "lo":
                    continue
                state_p = os.path.join(p, "operstate")
                state   = open(state_p).read().strip() if os.path.exists(state_p) else "unknown"
                ifaces.append({"name": name, "state": state})
            return {"interfaces": ifaces, "count": len(ifaces)} if ifaces else None
        except Exception:
            _warn_once("network", "NetworkMetaSource: read failed")
            return None


# ── DIAGNOSTIC SOURCES (Phase 7) — read-only subsystem self-inspection ─────────
# A diagnostic lets an agent SEE a subsystem's failure state BEFORE any fix is
# attempted. Every diagnostic is READ-ONLY (reads /proc, /sys, or a read-only
# query to a standard tool) and NEVER mutates state. All are Linux-specific;
# each degrades gracefully where its source is absent (observable=False + reason,
# NEVER an unhandled FileNotFoundError), so a diagnostic on a machine without the
# subsystem answers honestly instead of crashing.
#
# READING CONTRACT (shared): every diagnostic reading carries
#   observable : bool  — could the primary signal actually be inspected here?
#   reason     : str   — "" when fully observable; else why it degraded.
# plus a boolean "state" field (present / loaded / active / …) that is the
# condition-subscribable field. "unknown" is modelled as the boolean's safe
# default (e.g. present=False) WITH observable=False + reason — this keeps the
# field a real boolean so an eq-on-bool condition validates & evaluates cleanly,
# rather than overloading it with a string sentinel.

class _DiagnosticSource(SignalSource):
    """Base for a read-only diagnostic. Subclasses implement _observe() and must
    never raise; read() is the never-raise firewall that turns any unexpected
    failure into an honest observable=False reading (not a crash)."""

    family: str = "diagnostic"

    def _base(self) -> dict:
        return {"observable": False, "reason": ""}

    def _observe(self) -> dict:
        raise NotImplementedError

    def read(self) -> dict | None:
        try:
            return self._observe()
        except Exception as e:
            _warn_once(self.family, f"{self.family}: read failed")
            out = self._base()
            out["reason"] = f"unexpected error: {type(e).__name__}"
            return out


def _fd_holders(target_real: str, limit: int = 16) -> list[int]:
    """PIDs holding an open fd on `target_real`, via a read-only /proc/*/fd scan.
    Best-effort: fds owned by other users are invisible without privilege; those
    processes are silently skipped (a permission error is not a failure here)."""
    holders: list[int] = []
    if not os.path.isdir("/proc"):
        return holders
    for pid_dir in glob.glob("/proc/[0-9]*/fd"):
        try:
            pid = int(os.path.basename(os.path.dirname(pid_dir)))
        except ValueError:
            continue
        try:
            for fd in os.listdir(pid_dir):
                try:
                    if os.path.realpath(os.path.join(pid_dir, fd)) == target_real:
                        holders.append(pid)
                        break
                except OSError:
                    continue
        except OSError:
            # /proc/<pid>/fd unreadable (other user, or process gone) — skip.
            continue
        if len(holders) >= limit:
            break
    return sorted(holders)


class DeviceNodeHealthSource(_DiagnosticSource):
    """Existence + permissions of a device node (e.g. /dev/video0) and which PIDs
    hold it open. Reads the filesystem + /proc only; NEVER opens the device."""

    family = "device_node_health"
    name   = "device_node_health"

    def __init__(self, path: str) -> None:
        self.path = path

    def _observe(self) -> dict:
        out = self._base()
        out["path"] = self.path
        # The filesystem always answers existence/permissions on any POSIX host,
        # so the primary signal is observable everywhere; only the holder scan can
        # degrade (no /proc), which we note without failing the whole reading.
        out["observable"] = True
        present  = os.path.exists(self.path)
        out["present"]  = present
        out["readable"] = bool(present and os.access(self.path, os.R_OK))
        if present:
            target_real = os.path.realpath(self.path)
            out["holder_pids"] = _fd_holders(target_real)
            if not os.path.isdir("/proc"):
                out["reason"] = "/proc absent — holder PIDs not scannable"
        else:
            out["holder_pids"] = []
        # Scalar mirror of holder_pids so a condition (and the Phase 8 intervention
        # VERIFY) can check "the node has been released" as holder_count == 0.
        out["holder_count"] = len(out["holder_pids"])
        return out


class KernelModuleHealthSource(_DiagnosticSource):
    """Whether a named kernel module is loaded (/proc/modules) plus dmesg tail
    lines mentioning it. dmesg often needs privilege (kernel.dmesg_restrict): we
    degrade its lines honestly (dmesg_available=False) but keep `loaded`
    observable from /proc/modules regardless. Read-only throughout."""

    family = "kernel_module_health"
    name   = "kernel_module_health"

    def __init__(self, module: str, dmesg_tail: int = 5) -> None:
        self.module     = module
        self.dmesg_tail = dmesg_tail

    def _observe(self) -> dict:
        out = self._base()
        out["module"]          = self.module
        out["dmesg_available"] = False
        out["dmesg_lines"]     = []

        mods_path = "/proc/modules"
        if not os.path.exists(mods_path):
            # Source absent (non-Linux / no procfs): loaded is UNKNOWN — safe
            # default False, flagged by observable=False so False≠"confirmed off".
            out["loaded"]   = False
            out["reason"]   = "/proc/modules absent — module state unknown"
            return out

        out["observable"] = True
        norm = self.module.replace("-", "_")
        loaded = False
        try:
            with open(mods_path) as f:
                for line in f:
                    first = line.split(" ", 1)[0]
                    if first == self.module or first.replace("-", "_") == norm:
                        loaded = True
                        break
        except OSError:
            out["loaded"] = False
            out["reason"] = "/proc/modules unreadable — module state unknown"
            return out
        out["loaded"] = loaded

        # dmesg is a read-only query; capture the tail lines mentioning the module.
        if shutil.which("dmesg"):
            try:
                r = subprocess.run(["dmesg", "--notime"], capture_output=True,
                                   text=True, timeout=5)
                if r.returncode == 0:
                    out["dmesg_available"] = True
                    hits = [ln for ln in r.stdout.splitlines()
                            if norm in ln.replace("-", "_")]
                    out["dmesg_lines"] = hits[-self.dmesg_tail:]
                else:
                    out["reason"] = "dmesg requires privilege (kernel.dmesg_restrict)"
            except Exception:
                out["reason"] = "dmesg unavailable"
        else:
            out["reason"] = "dmesg not installed"
        return out


class ServiceHealthSource(_DiagnosticSource):
    """A systemd unit's active-state via `systemctl is-active` / `show` (read-only
    queries). Supports --user units. Degrades to observable=False where systemctl
    is absent (non-systemd host)."""

    family = "service_health"
    name   = "service_health"

    def __init__(self, service: str, user: bool = False) -> None:
        self.service = service
        self.user    = user

    def _observe(self) -> dict:
        out = self._base()
        out["service"] = self.service
        out["scope"]   = "user" if self.user else "system"
        out["active_state"] = "unknown"
        out["sub_state"]    = "unknown"

        if not shutil.which("systemctl"):
            out["active"] = False
            out["reason"] = "systemctl absent — not a systemd host"
            return out

        out["observable"] = True
        scope = ["--user"] if self.user else []
        try:
            r = subprocess.run(["systemctl", *scope, "show", self.service,
                                "-p", "ActiveState", "-p", "SubState"],
                               capture_output=True, text=True, timeout=5)
            props: dict[str, str] = {}
            for line in r.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            out["active_state"] = props.get("ActiveState", "unknown") or "unknown"
            out["sub_state"]    = props.get("SubState", "unknown") or "unknown"
            out["active"]       = (out["active_state"] == "active")
            if out["active_state"] == "unknown":
                out["reason"] = "unit not found or state unavailable"
        except Exception:
            out["active"] = False
            out["reason"] = "systemctl query failed"
        return out


class UsbPowerHealthSource(_DiagnosticSource):
    """Autosuspend / runtime power state of a USB device from sysfs
    (/sys/bus/usb/devices/<dev>/power/*). Read-only sysfs reads only — writing
    control here would CHANGE power policy, which we never do. `dev` is a bus id
    like '3-2' or an absolute sysfs device path."""

    family = "usb_power_health"
    name   = "usb_power_health"

    def __init__(self, dev: str) -> None:
        # Accept either a bus id ('3-2') or a full sysfs device path.
        if os.path.isabs(dev):
            self.dev_path = dev
        else:
            self.dev_path = os.path.join("/sys/bus/usb/devices", dev)
        self.dev = dev

    def _read_attr(self, name: str) -> str | None:
        p = os.path.join(self.dev_path, "power", name)
        try:
            return open(p).read().strip()
        except OSError:
            return None

    def _observe(self) -> dict:
        out = self._base()
        out["path"] = self.dev_path
        out["control"]              = "unknown"
        out["runtime_status"]       = "unknown"
        out["autosuspend_delay_ms"] = -1

        if not os.path.isdir(os.path.join(self.dev_path, "power")):
            out["present"]     = False
            out["autosuspend"] = False
            out["reason"]      = "USB device power sysfs absent"
            return out

        out["observable"] = True
        out["present"]    = True
        control = self._read_attr("control")
        if control is not None:
            out["control"]     = control
            out["autosuspend"] = (control == "auto")
        else:
            out["autosuspend"] = False
        status = self._read_attr("runtime_status")
        if status is not None:
            out["runtime_status"] = status
        delay = self._read_attr("autosuspend_delay_ms")
        if delay is not None:
            try:
                out["autosuspend_delay_ms"] = int(delay)
            except ValueError:
                pass
        return out


# family → SignalSource class + which __init__ kwarg names the target. The device
# runtime's attach_diagnostic uses this to build a diagnostic from a family name.
DIAGNOSTIC_SOURCES: dict[str, type] = {
    "device_node_health":   DeviceNodeHealthSource,
    "kernel_module_health": KernelModuleHealthSource,
    "service_health":       ServiceHealthSource,
    "usb_power_health":     UsbPowerHealthSource,
}
