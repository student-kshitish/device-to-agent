"""
d2a/resource_probes.py — generic physical resource probes.

Each probe detects AVAILABILITY ONLY. No frames are captured, no audio recorded,
no location computed. Probing is completely side-effect-free and privacy-safe.

Sensitive resources carry "access": "owner_consent" to signal that an explicit
policy decision is required before any agent can use them.
"""

import glob
import os
import shutil


# ── Sensitivity classification ─────────────────────────────────────────────────
# OPEN     : bindable by any trusted remote agent by default.
# sensitive: requires explicit owner consent before any remote agent can bind.

RESOURCE_SENSITIVITY: dict[str, str] = {
    # open — safe to expose remotely
    "compute":       "open",
    "gpu":           "open",
    "sensing":       "open",
    "battery_aware": "open",
    "storage":       "open",
    "network":       "open",
    # sensitive — denied to remote agents until owner opts in
    "camera":        "sensitive",
    "microphone":    "sensitive",
    "location":      "sensitive",
    "display":       "sensitive",
}


# ── Diagnostic consent SSOT (Phase 7) ──────────────────────────────────────────
# Diagnostics are a READ-ONLY self-inspection family (see d2a/stream_source.py):
# each reads a subsystem's health via read-only means only (no state ever
# changes). They are ALL sensitive: system introspection reveals running
# processes (fd holders), the module/service/device inventory of the host — so a
# remote agent is denied by default and needs explicit owner approval, exactly
# like camera/microphone. Keyed by diagnostic FAMILY (a concrete diagnostic
# capability is named diag_<family>_<target-slug>; its policy rule is set per
# capability at attach time, but the family's intrinsic tier lives HERE so the
# manifest consent_tier can be validated against the one SSOT).
DIAGNOSTIC_SENSITIVITY: dict[str, str] = {
    "device_node_health":   "sensitive",
    "kernel_module_health": "sensitive",
    "service_health":       "sensitive",
    "usb_power_health":     "sensitive",
}


# ── Intervention consent SSOT (Phase 8) ─────────────────────────────────────────
# Interventions MUTATE device state (restart a service, kill a holder, load a
# module). A wrong intervention can worsen things with NO undo, so this is a THIRD
# consent tier above "sensitive": "intervention". It is deny-by-default with a
# DOUBLE GATE — binding an intervention capability needs owner approval (the right
# to PROPOSE), and every concrete plan needs its own per-plan owner approval before
# anything executes. Keyed by intervention FAMILY; the concrete capability is named
# intv_<family>_<target-slug>. The tier lives HERE (the one SSOT) so a manifest's
# consent_tier can be validated against it, exactly like the other two families.
INTERVENTION_SENSITIVITY: dict[str, str] = {
    "service_intervene":       "intervention",
    "process_release":         "intervention",
    "kernel_module_intervene": "intervention",
}


# ── Individual probes ──────────────────────────────────────────────────────────

def probe_camera() -> dict | None:
    """
    Detect camera device PRESENCE only. Does NOT open the device or capture frames.
    Returns None if no camera nodes found.
    """
    try:
        nodes = sorted(glob.glob("/dev/video*"))
        if not nodes:
            return None
        return {
            "count":  len(nodes),
            "nodes":  nodes[:4],     # up to 4 node paths
            "access": "owner_consent",
        }
    except Exception:
        return None


def probe_microphone() -> dict | None:
    """
    Detect microphone presence via ALSA card list and capture device nodes.
    Does NOT record or open any audio stream.
    Returns None if no sound hardware found.
    """
    try:
        cards_path = "/proc/asound/cards"
        has_cards  = False
        if os.path.exists(cards_path):
            content   = open(cards_path).read().strip()
            has_cards = bool(content) and "no soundcards" not in content.lower()

        capture_nodes = glob.glob("/dev/snd/pcmC*D*c")

        if not has_cards and not capture_nodes:
            return None

        return {
            "present":       True,
            "capture_nodes": len(capture_nodes),
            "access":        "owner_consent",
        }
    except Exception:
        return None


def probe_location() -> dict | None:
    """
    Detect GPS capability via serial device nodes (/dev/ttyACM*, /dev/ttyUSB*).
    These are common GPS dongles but uncertain — noted as candidates only.
    Does NOT read any GPS data or geolocate at probe time.
    Returns None if no GPS candidate nodes found.
    """
    try:
        candidates = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        if not candidates:
            return None
        return {
            "present": True,
            "source":  "gps_serial_candidate",
            "nodes":   candidates[:4],
            "access":  "owner_consent",
        }
    except Exception:
        return None


def probe_storage() -> dict | None:
    """
    Collect disk usage for root filesystem and any mounted volumes under /mnt and /media.
    Pure stat() calls — no file content read. Access level: open.
    """
    try:
        root = shutil.disk_usage("/")
        mounts = [{
            "path":      "/",
            "total_gb":  round(root.total / 1e9, 1),
            "free_gb":   round(root.free  / 1e9, 1),
            "used_pct":  round(root.used  / root.total * 100, 1),
        }]
        for base in ("/mnt", "/media"):
            if not os.path.isdir(base):
                continue
            try:
                for entry in os.listdir(base):
                    path = os.path.join(base, entry)
                    try:
                        u = shutil.disk_usage(path)
                        if u.total > 0:
                            mounts.append({
                                "path":     path,
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
            "access":   "open",
        }
    except Exception:
        return None


def probe_network() -> dict | None:
    """
    Enumerate network interfaces from /sys/class/net, skipping loopback.
    Does NOT expose traffic contents or packet captures.
    """
    try:
        ifaces = []
        for p in sorted(glob.glob("/sys/class/net/*")):
            name = os.path.basename(p)
            if name != "lo":
                ifaces.append(name)
        if not ifaces:
            return None
        return {
            "interfaces": ifaces,
            "access":     "open",
        }
    except Exception:
        return None


def probe_display() -> dict | None:
    """
    Detect connected display outputs via DRM sysfs. Does NOT capture screenshots.
    Returns None if no DRM outputs are found.
    """
    try:
        status_paths = glob.glob("/sys/class/drm/*/status")
        if not status_paths:
            return None
        statuses: dict = {}
        for p in sorted(status_paths):
            name = os.path.basename(os.path.dirname(p))
            try:
                statuses[name] = open(p).read().strip()
            except Exception:
                pass
        if not statuses:
            return None
        connected = sum(1 for s in statuses.values() if s == "connected")
        return {
            "connected_outputs": connected,
            "total_outputs":     len(statuses),
            "access":            "owner_consent",
        }
    except Exception:
        return None


# ── Registry + aggregator ──────────────────────────────────────────────────────

RESOURCE_PROBES: dict = {
    "camera":     probe_camera,
    "microphone": probe_microphone,
    "location":   probe_location,
    "storage":    probe_storage,
    "network":    probe_network,
    "display":    probe_display,
}


def probe_resources() -> dict:
    """
    Run all resource probes and return a dict of present resources.
    Absent resources are omitted (None return = not present).
    Each entry carries an "access" hint: "open" or "owner_consent".
    """
    result: dict = {}
    for name, fn in RESOURCE_PROBES.items():
        try:
            data = fn()
            if data is not None:
                result[name] = data
        except Exception:
            pass
    return result
