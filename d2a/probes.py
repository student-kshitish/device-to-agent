import glob
import os
import platform
import shutil
import subprocess
import time


def probe_cpu() -> dict | None:
    try:
        return {"count": os.cpu_count(), "arch": platform.machine()}
    except Exception:
        return None


def probe_loadavg() -> dict | None:
    try:
        if os.path.exists("/proc/loadavg"):
            parts = open("/proc/loadavg").read().split()
            return {"load1": float(parts[0]), "load5": float(parts[1]), "load15": float(parts[2])}
        load = os.getloadavg()
        return {"load1": load[0], "load5": load[1], "load15": load[2]}
    except Exception:
        return None


def probe_memory() -> dict | None:
    try:
        if not os.path.exists("/proc/meminfo"):
            return None
        data = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, val = line.split(":")
                data[key.strip()] = int(val.split()[0])
        total_kb = data.get("MemTotal", 0)
        avail_kb = data.get("MemAvailable", 0)
        return {
            "total_mb":    round(total_kb / 1024, 1),
            "available_mb": round(avail_kb / 1024, 1),
            "used_percent": round((total_kb - avail_kb) / total_kb * 100, 1) if total_kb else 0,
        }
    except Exception:
        return None


def probe_disk() -> dict | None:
    try:
        u = shutil.disk_usage("/")
        return {
            "total_gb": round(u.total / 1e9, 1),
            "free_gb": round(u.free / 1e9, 1),
            "used_pct": round(u.used / u.total * 100, 1),
        }
    except Exception:
        return None


def probe_battery() -> dict | None:
    try:
        bats = glob.glob("/sys/class/power_supply/BAT*")
        if not bats:
            return None
        bat = bats[0]
        cap_path = os.path.join(bat, "capacity")
        stat_path = os.path.join(bat, "status")
        if not os.path.exists(cap_path):
            return None
        capacity = int(open(cap_path).read().strip())
        status = open(stat_path).read().strip() if os.path.exists(stat_path) else "unknown"
        return {"capacity_pct": capacity, "status": status, "path": os.path.basename(bat)}
    except Exception:
        return None


def probe_thermal() -> dict | None:
    try:
        paths = sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))
        if not paths:
            return None
        temps = []
        for p in paths:
            try:
                temps.append(round(int(open(p).read().strip()) / 1000, 1))
            except Exception:
                pass
        return {"temps_c": temps, "zone_count": len(temps)} if temps else None
    except Exception:
        return None


def probe_gpu() -> dict | None:
    # nvidia
    try:
        if shutil.which("nvidia-smi"):
            r = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,memory.total,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                parts = [p.strip() for p in r.stdout.strip().split(",")]
                return {
                    "vendor": "nvidia",
                    "name": parts[0],
                    "vram_mib": int(parts[1]),
                    "util_pct": int(parts[2]),
                }
    except Exception:
        pass

    # amd via rocm-smi
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

    # amd via sysfs vendor id
    try:
        for p in glob.glob("/sys/class/drm/card*/device/vendor"):
            if open(p).read().strip() == "0x1002":
                return {"vendor": "amd", "present": True}
    except Exception:
        pass

    # generic drm presence
    try:
        cards = glob.glob("/sys/class/drm/card*/device")
        if cards:
            return {"vendor": "unknown", "present": True, "card_count": len(cards)}
    except Exception:
        pass

    return None


def probe_sensors() -> dict | None:
    try:
        inputs = []
        for pat in [
            "/sys/class/hwmon/*/temp*_input",
            "/sys/class/hwmon/*/fan*_input",
            "/sys/class/hwmon/*/in*_input",
            "/sys/class/hwmon/*/curr*_input",
        ]:
            inputs.extend(glob.glob(pat))
        if not inputs:
            return None
        hwmon_names = {}
        for p in glob.glob("/sys/class/hwmon/*/name"):
            try:
                hwmon_names[os.path.dirname(p)] = open(p).read().strip()
            except Exception:
                pass
        return {
            "count": len(inputs),
            "hwmon_count": len(hwmon_names),
            "hwmons": list(hwmon_names.values()),
        }
    except Exception:
        return None


PROBES: dict = {
    "cpu":     probe_cpu,
    "loadavg": probe_loadavg,
    "memory":  probe_memory,
    "disk":    probe_disk,
    "battery": probe_battery,
    "thermal": probe_thermal,
    "gpu":     probe_gpu,
    "sensors": probe_sensors,
}


def probe_all() -> dict:
    snapshot: dict = {}
    for key, fn in PROBES.items():
        try:
            result = fn()
            if result is not None:
                snapshot[key] = result
        except Exception:
            pass

    has_battery = "battery" in snapshot
    has_gpu     = "gpu" in snapshot
    has_thermal = "thermal" in snapshot
    cpu_count   = (snapshot.get("cpu") or {}).get("count", 0) or 0

    if has_battery and has_gpu:
        device_class = "laptop"
    elif has_battery and not has_gpu:
        device_class = "mobile_or_handheld"
    elif not has_battery and has_gpu:
        device_class = "workstation_or_server"
    elif has_thermal and cpu_count <= 4 and not has_gpu:
        device_class = "sbc_or_pi"
    else:
        device_class = "generic"

    snapshot["device_class"] = device_class
    snapshot["timestamp"]    = time.time()
    return snapshot


def available_resources(snapshot: dict) -> list[str]:
    resources = ["compute"]
    if "gpu" in snapshot:
        resources.append("gpu")
    if "thermal" in snapshot or "sensors" in snapshot:
        resources.append("sensing")
    if "battery" in snapshot:
        resources.append("battery_aware")
    return resources
