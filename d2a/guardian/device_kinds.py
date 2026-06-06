"""
d2a/guardian/device_kinds.py — Device-kind detector for the Capability Guardian.

SECURITY CONTRACT:
- detect_kind() operates ONLY on the path the user EXPLICITLY provides.
  It NEVER auto-scans /dev, /sys, or any directory on its own.
- INPUT-EVENT devices (/dev/input/event*) are SENSITIVE by design.
  Reading a live keystroke stream is functionally a keylogger.  This module
  marks that kind "sensitive" so the relay enforces consent-gating.  The
  LEGITIMATE purpose is PHYSICAL CONTROL INPUT for agents: game controllers,
  button boxes, barcode scanners, assistive/adaptive devices, robot
  teleoperation.  NOT for general keyboard/mouse capture.
- is_system_input() flags event devices that look like the host's primary
  keyboard/mouse (vs a dedicated controller), triggering EXTRA sensitive
  marking in the relay capability record.
- Raw-device access needs OS-level permissions.  Real remote deployments MUST
  enforce strong identity (e.g., Ed25519 signing) before granting relay access.

Detection uses os.stat() mode bits and path patterns only — never reads
device contents, never raises, always returns a defined kind constant.
"""

import os
import stat
import re


# ── kind constants ────────────────────────────────────────────────────────────

KIND_BLOCK_FS    = "block_fs"     # mounted directory / block device with filesystem
KIND_CHAR_STREAM = "char_stream"  # serial/char device (/dev/ttyUSB*, /dev/ttyACM*, …)
KIND_INPUT_EVENT = "input_event"  # input node (/dev/input/event*) — SENSITIVE
KIND_SENSOR_FILE = "sensor_file"  # sysfs/proc scalar value file (single-read)
KIND_RAW_GENERIC = "raw_generic"  # exists but matches none of the above
KIND_UNAVAILABLE = "unavailable"  # missing or unreadable

# Primitives offered per kind — single source of truth shared by relay, runtime, and synthesizer
KIND_PRIMITIVES: dict[str, list[str]] = {
    KIND_BLOCK_FS:    ["list_entries", "read_bytes", "write_bytes", "stat", "delete"],
    KIND_CHAR_STREAM: ["open_stream", "read_stream", "write_stream", "close_stream"],
    KIND_INPUT_EVENT: ["read_events"],
    KIND_SENSOR_FILE: ["read_value"],
    KIND_RAW_GENERIC: ["read_bytes", "write_bytes"],
    KIND_UNAVAILABLE: [],
}

# Sensitivity mapping — never downgrade input_event to "open"
KIND_SENSITIVITY: dict[str, str] = {
    KIND_BLOCK_FS:    "open",
    KIND_CHAR_STREAM: "open",
    KIND_INPUT_EVENT: "sensitive",  # consent required; DumbRelay enforces this
    KIND_SENSOR_FILE: "open",
    KIND_RAW_GENERIC: "open",
    KIND_UNAVAILABLE: "open",       # moot — device absent
}

# Serial/stream character devices (GPS, Arduino, scanner, serial sensor, …)
_CHAR_STREAM_RE = re.compile(
    r"/dev/(ttyUSB|ttyACM|ttyS|ttyTHS|ttyAMA|ttyHS|rfcomm|cu\.\w+)\d*",
    re.IGNORECASE,
)

# Linux input-event device nodes
_INPUT_EVENT_RE = re.compile(r"/dev/input/event\d+$")

# Sysfs / procfs scalar-file roots (sensor readings, hwmon, iio, thermal zones)
_SENSOR_PATH_RE = re.compile(
    r"^(/sys/|/proc/|.*/hwmon\d+/|.*/thermal_zone\d+/|.*/iio:device\d+/)"
)


def detect_kind(path: str) -> str:
    """
    Return the functional kind of the device at *path*.

    Branches (tried in order):
      1. dir / block node  → block_fs   (mounted fs or raw block)
      2. char node at /dev/input/event* → input_event  [SENSITIVE]
      3. char node at tty/serial path   → char_stream
      4. any other char node            → raw_generic
      5. regular file under /sys /proc  → sensor_file
      6. any other regular file         → raw_generic
      7. anything else (FIFO, socket)   → raw_generic
      8. stat fails                     → unavailable

    Never raises; all branches are try/except-guarded.
    """
    if not path:
        return KIND_UNAVAILABLE

    try:
        real = os.path.realpath(path)
        st   = os.stat(real)
        mode = st.st_mode
    except OSError:
        return KIND_UNAVAILABLE

    # ── directory or block device (mounted filesystem) ────────────────────────
    if stat.S_ISDIR(mode) or stat.S_ISBLK(mode):
        return KIND_BLOCK_FS

    # ── character device — subtype by path pattern ───────────────────────────
    if stat.S_ISCHR(mode):
        real_str = str(real)

        # Input-event node — SENSITIVE (keyboard / mouse / controller / scanner)
        if _INPUT_EVENT_RE.search(real_str):
            return KIND_INPUT_EVENT

        # Serial / stream device (GPS, Arduino, barcode, …)
        if _CHAR_STREAM_RE.search(real_str):
            return KIND_CHAR_STREAM

        # Other char nodes (/dev/null, /dev/video*, /dev/random, …)
        return KIND_RAW_GENERIC

    # ── regular file ─────────────────────────────────────────────────────────
    if stat.S_ISREG(mode):
        real_str = str(real)

        # Small file under a sysfs/proc path → sensor scalar
        if _SENSOR_PATH_RE.match(real_str):
            try:
                # sysfs files may report size=0; still valid sensors
                if st.st_size < 4096 or st.st_size == 0:
                    return KIND_SENSOR_FILE
            except OSError:
                pass

        return KIND_RAW_GENERIC

    # ── everything else (FIFO, socket, …) ────────────────────────────────────
    return KIND_RAW_GENERIC


def is_system_input(path: str) -> bool:
    """
    Best-effort heuristic: does this input_event path look like the HOST's
    primary keyboard or mouse (vs a dedicated controller / button box)?

    Returns True for /dev/input/event0..event3 — these slots are typically
    occupied by the core keyboard and mouse on most Linux desktops.
    Higher event numbers are usually gamepads, controllers, button boxes.

    This is advisory only: human consent is ALWAYS required for input_event
    regardless of this flag.  The flag merely triggers EXTRA SENSITIVE
    marking in the relay capability record so the guardian agent can surface
    a stronger warning to the user.

    Pure path-string analysis — no stat, no file reads, never raises.
    """
    try:
        real     = os.path.realpath(path)
        real_str = str(real)

        if not _INPUT_EVENT_RE.search(real_str):
            return False  # not an input-event node at all

        # Extract the trailing event number
        num_str = real_str.rsplit("event", 1)[-1]
        if num_str.isdigit():
            return int(num_str) <= 3  # event0–3 → likely system input

        return True  # can't parse number → err on the side of caution
    except Exception:
        return False
