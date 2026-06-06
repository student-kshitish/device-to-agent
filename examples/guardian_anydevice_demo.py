"""
examples/guardian_anydevice_demo.py — CASE 2 GENERALIZED: Guardian for ANY dumb peripheral.

Architecture (same as guardian_demo.py, now device-agnostic):
    DumbRelay  (host where device is plugged):
        - auto-detects device kind via os.stat() mode bits + path patterns
        - exposes ONLY primitives valid for that kind
        - falls back to raw_generic for anything unrecognised — never crashes

    GuardianAgent  (runs anywhere, remote from device):
        - attaches to relay, auto-selects kind-appropriate skills
        - ALL intelligence lives here, never on the hardware

    VirtualSmartObject  (what the network sees):
        - name / tags / actions reflect the device kind
        - routes high-level requests → guardian skill → relay primitive → device

SIMULATED STAND-INS (clearly marked):
    Each device kind is simulated with a temp file / temp dir populated with
    representative data.  On a real host, point the relay at the actual /dev
    or sysfs path instead — only the path changes, nothing else.

SECURITY ENFORCED THROUGHOUT:
    - Input-event devices are DENIED by default (consent_required).
    - System keyboard/mouse paths flagged as EXTRA sensitive.
    - Scope jail: block_fs uses realpath jail; char/sensor/input use exact-path.
    - No auto-scanning: every relay is pointed at ONE explicit path.
"""

import os
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d2a.guardian.device_kinds import detect_kind, is_system_input, KIND_SENSITIVITY
from d2a.guardian.relay import DumbRelay
from agents.guardian_agent import GuardianAgent
from d2a.guardian.virtual_object import VirtualSmartObject

DIVIDER = "=" * 70


def section(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def show(label: str, value) -> None:
    print(f"    {label:<24} {value}")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS — build fake device data for each simulated kind
# ─────────────────────────────────────────────────────────────────────────────

def _make_block_fs() -> str:
    """Simulate a USB/SD card mount: temp dir with three files."""
    d = tempfile.mkdtemp(prefix="d2a_blk_")
    for name, content in [
        ("readme.txt",  b"Device README\n"),
        ("data.csv",    b"ts,val\n1,42\n2,43\n"),
        ("config.json", b'{"version":1}\n'),
    ]:
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(content)
    return d


def _make_char_stream() -> str:
    """
    Simulate a GPS serial device (real: /dev/ttyUSB0).
    Pre-populate with NMEA-0183 sentences — on a real device these arrive
    as a live byte stream.  kind_override='char_stream' tells the relay to
    treat this temp file like a character device.
    """
    fh = tempfile.NamedTemporaryFile(delete=False, suffix=".nmea", mode="wb",
                                     prefix="d2a_uart_")
    fh.write(
        b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n"
        b"$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A\r\n"
        b"$GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1*39\r\n"
        b"$GPGSV,2,1,08,01,40,083,46,02,17,308,41,12,07,344,39,14,22,228,45*75\r\n"
    )
    fh.close()
    return fh.name


def _make_sensor_file(value: str = "42.5") -> str:
    """
    Simulate a sysfs temperature sensor (real: /sys/class/thermal/thermal_zone0/temp).
    kind_override='sensor_file' makes the relay treat this like a sysfs entry.
    """
    fh = tempfile.NamedTemporaryFile(delete=False, suffix=".sensor", mode="w",
                                     prefix="d2a_sensor_")
    fh.write(value + "\n")
    fh.close()
    return fh.name


def _make_raw_generic() -> str:
    """Simulate an unknown binary device node — raw bytes, no recognized structure."""
    fh = tempfile.NamedTemporaryFile(delete=False, suffix=".bin", prefix="d2a_raw_")
    fh.write(bytes(range(128)) * 2)   # 256 bytes of deterministic pattern
    fh.close()
    return fh.name


def _make_input_event_file(n_events: int = 4) -> str:
    """
    Simulate an input event device (real: /dev/input/event5 for a game controller).
    Each fake event is a 24-byte Linux struct input_event:
      tv_sec(u64) tv_usec(u64) type(u16) code(u16) value(i32)
    kind_override='input_event' makes the relay treat this like an event node.
    """
    fmt = "<QQHHi"   # 24 bytes
    events = [
        (0, 0, 1, 304, 1),   # EV_KEY  button 304 pressed   (BTN_SOUTH / A button)
        (0, 1, 1, 304, 0),   # EV_KEY  button 304 released
        (0, 2, 3, 0,   128), # EV_ABS  abs_axis_0 = 128      (left-stick X center)
        (0, 3, 0, 0,   0),   # EV_SYN  sync marker
    ]
    fh = tempfile.NamedTemporaryFile(delete=False, suffix=".evdev", prefix="d2a_input_")
    for ev in events[:n_events]:
        fh.write(struct.pack(fmt, *ev))
    fh.close()
    return fh.name


# ═════════════════════════════════════════════════════════════════════════════
# TEST SUITE
# ═════════════════════════════════════════════════════════════════════════════

all_temps: list[str] = []   # collect paths for cleanup at end


# ─────────────────────────────────────────────────────────────────────────────
# BLOCK_FS (existing storage path — regression test + per-kind print)
# ─────────────────────────────────────────────────────────────────────────────
section("BLOCK_FS — mounted filesystem (simulates USB/SD mount)")

blk_path = _make_block_fs()
all_temps.append(blk_path)
print(f"  [sim] temp dir = {blk_path}  (real host: USB/SD mount path)")

relay_blk = DumbRelay("relay-blk", blk_path)
cap_blk   = relay_blk.capabilities()[0]
print(f"\nRELAY KIND=block_fs EXPOSES {cap_blk['primitives']}")

# TEST A: right primitives offered, wrong ops rejected
assert cap_blk["kind"] == "block_fs"
assert "list_entries" in cap_blk["primitives"]
assert "read_events"  not in cap_blk["primitives"]
bad = relay_blk.handle_op({"op": "read_events"})
assert "error" in bad, f"block_fs must reject read_events: {bad}"
print(f"  reject read_events  → {bad['error']}")

# TEST B: guardian skill from relay primitives
g_blk = GuardianAgent("guardian-blk")
g_blk.attach(cap_blk)
idx = g_blk.index()
assert idx["indexed"] == 3
srch = g_blk.search("data")
assert srch["count"] > 0
print(f"  guardian indexed {idx['indexed']} files, found: {srch['matches']}")
print("GUARDIAN MADE block_fs SMART")

# TEST C: VSO advertises
vso_blk = VirtualSmartObject(cap_blk, g_blk)
adv_blk = vso_blk.advertised_capability()
assert adv_blk["name"] == "smart_storage"
assert "indexed" in adv_blk["tags"]
print(f"  VSO name={adv_blk['name']}  tags={adv_blk['tags'][:3]}…")


# ─────────────────────────────────────────────────────────────────────────────
# CHAR_STREAM — serial/UART device
# ─────────────────────────────────────────────────────────────────────────────
section("CHAR_STREAM — serial device (simulates /dev/ttyUSB0 GPS receiver)")

uart_path = _make_char_stream()
all_temps.append(uart_path)
print(f"  [sim] temp file = {uart_path}  (real host: /dev/ttyUSB0 or /dev/ttyACM0)")

# kind_override='char_stream' — on a real device detect_kind() returns this automatically
relay_uart = DumbRelay("relay-uart", uart_path, kind_override="char_stream")
cap_uart   = relay_uart.capabilities()[0]
print(f"\nRELAY KIND=char_stream EXPOSES {cap_uart['primitives']}")

# TEST A: right primitives; wrong ops rejected
assert cap_uart["kind"]       == "char_stream"
assert "open_stream"          in cap_uart["primitives"]
assert "list_entries"         not in cap_uart["primitives"]
bad = relay_uart.handle_op({"op": "list_entries", "path": ""})
assert "error" in bad, f"char_stream must reject list_entries: {bad}"
print(f"  reject list_entries → {bad['error']}")

# TEST B: guardian collect + parse GPS
g_uart = GuardianAgent("guardian-uart")
g_uart.attach(cap_uart)
assert g_uart.skills == ["collect", "parse", "tail"]

coll = g_uart.collect(duration=0.5)
assert coll["bytes_collected"] > 0, f"collect returned: {coll}"
parsed = g_uart.parse(pattern="nmea")
assert parsed["count"] >= 2, f"expected NMEA sentences, got: {parsed}"
fix = parsed["records"][0]
print(f"  collected {coll['bytes_collected']} bytes, parsed {parsed['count']} NMEA sentences")
print(f"  first sentence: talker={fix['talker']}  type={fix['msg_type']}  → {fix['sentence'][:40]}…")
print("GUARDIAN MADE char_stream SMART")

# TEST C: VSO
vso_uart = VirtualSmartObject(cap_uart, g_uart)
adv_uart = vso_uart.advertised_capability()
assert adv_uart["name"] == "smart_sensor_stream"
assert "parsed" in adv_uart["tags"]
res = vso_uart.handle_request({"action": "parse", "pattern": "nmea"})
assert res["count"] >= 2
print(f"  VSO name={adv_uart['name']}  tags={adv_uart['tags']}")


# ─────────────────────────────────────────────────────────────────────────────
# SENSOR_FILE — sysfs scalar
# ─────────────────────────────────────────────────────────────────────────────
section("SENSOR_FILE — sysfs scalar (simulates /sys/class/thermal/thermal_zone0/temp)")

sensor_path = _make_sensor_file("68.0")
all_temps.append(sensor_path)
print(f"  [sim] temp file = {sensor_path}  (real host: sysfs / hwmon path)")
print(f"  [sim] initial value = 68.0  (will step up to trigger caution/danger thresholds)")

relay_sensor = DumbRelay("relay-sensor", sensor_path, kind_override="sensor_file")
cap_sensor   = relay_sensor.capabilities()[0]
print(f"\nRELAY KIND=sensor_file EXPOSES {cap_sensor['primitives']}")

# TEST A: right primitives; wrong ops rejected
assert cap_sensor["kind"]   == "sensor_file"
assert "read_value"         in cap_sensor["primitives"]
assert "list_entries"       not in cap_sensor["primitives"]
bad = relay_sensor.handle_op({"op": "list_entries", "path": ""})
assert "error" in bad
print(f"  reject list_entries → {bad['error']}")

# TEST B: guardian monitor + verdict
g_sensor = GuardianAgent("guardian-sensor")
g_sensor.attach(cap_sensor)
assert "monitor" in g_sensor.skills
assert "verdict" in g_sensor.skills

series = g_sensor.monitor(intervals=3, delay=0.0)
assert series["count"] == 3
vals = [s["value"] for s in series["series"]]
print(f"  monitor(3 polls) → {vals}")

verd_low = g_sensor.verdict(warn_threshold=75.0, danger_threshold=90.0)
assert verd_low["level"] == "good"

# Simulate temperature spike
with open(sensor_path, "w") as fh:
    fh.write("82.3\n")
verd_warn = g_sensor.verdict(warn_threshold=75.0, danger_threshold=90.0)
assert verd_warn["level"] == "caution"

with open(sensor_path, "w") as fh:
    fh.write("95.1\n")
verd_danger = g_sensor.verdict(warn_threshold=75.0, danger_threshold=90.0)
assert verd_danger["level"] == "danger"

print(f"  verdict(68°) = {verd_low['level']}   verdict(82°) = {verd_warn['level']}   verdict(95°) = {verd_danger['level']}")
print("GUARDIAN MADE sensor_file SMART")

# TEST C: VSO
vso_sensor = VirtualSmartObject(cap_sensor, g_sensor)
adv_sensor = vso_sensor.advertised_capability()
assert adv_sensor["name"] == "smart_sensor"
assert "monitored" in adv_sensor["tags"]
res = vso_sensor.handle_request({"action": "verdict", "warn_threshold": "75.0"})
assert res["level"] == "danger"
print(f"  VSO name={adv_sensor['name']}  tags={adv_sensor['tags']}")


# ─────────────────────────────────────────────────────────────────────────────
# INPUT_EVENT — TEST D1: DENIED BY DEFAULT (consent_required)
# ─────────────────────────────────────────────────────────────────────────────
section("INPUT_EVENT — TEST D1: relay created WITHOUT consent (should deny all ops)")

ev_path = _make_input_event_file()
all_temps.append(ev_path)
print(f"  [sim] temp file = {ev_path}  (real host: /dev/input/event5 for a game controller)")
print("  SECURITY: input_event kind is DENIED BY DEFAULT — consent_granted=False")

relay_ev_no_consent = DumbRelay("relay-ev-nocon", ev_path,
                                kind_override="input_event", consent_granted=False)
cap_ev_nc = relay_ev_no_consent.capabilities()[0]

# capabilities() must list the device with consent_required even without consent
assert cap_ev_nc["kind"]   == "input_event"
assert cap_ev_nc["access"] == "consent_required"
show("kind",          cap_ev_nc["kind"])
show("access",        cap_ev_nc["access"])
show("primitives",    cap_ev_nc["primitives"])

# read_events must return consent_required
r = relay_ev_no_consent.handle_op({"op": "read_events", "max_events": 4})
assert r.get("error") == "consent_required", f"expected consent_required, got: {r}"
print(f"\n  relay read_events  → error={r['error']}")

# decode_events through guardian must also propagate consent_required
g_ev_nc = GuardianAgent("guardian-ev-nc")
g_ev_nc.attach(cap_ev_nc)
decode_r = g_ev_nc.decode_events()
assert decode_r.get("error") == "consent_required"
print(f"  guardian decode_events → error={decode_r['error']}")

# VSO refuses the request too
vso_ev_nc = VirtualSmartObject(cap_ev_nc, g_ev_nc)
req_r = vso_ev_nc.handle_request({"action": "decode_events"})
assert req_r.get("error") == "consent_required"
print(f"  VSO decode_events → error={req_r['error']}")

print("\nINPUT DEVICE DENIED BY DEFAULT")


# ─────────────────────────────────────────────────────────────────────────────
# INPUT_EVENT — TEST D2: WITH CONSENT (game controller, not system keyboard)
# ─────────────────────────────────────────────────────────────────────────────
section("INPUT_EVENT — TEST D2: WITH consent_granted=True for a non-system controller")

print(f"  [sim] same event file, controller-style events (button press / abs axis)")
print("  LEGITIMATE USE: physical-control input for a game controller / button box")

relay_ev = DumbRelay("relay-ev-ctrl", ev_path,
                     kind_override="input_event", consent_granted=True)
cap_ev   = relay_ev.capabilities()[0]

assert cap_ev["access"] == "open"
assert cap_ev["system_input"] is False
show("kind",          cap_ev["kind"])
show("access",        cap_ev["access"])
show("system_input",  cap_ev["system_input"])

g_ev = GuardianAgent("guardian-ev-ctrl")
g_ev.attach(cap_ev)
assert "decode_events" in g_ev.skills

# read_events delivers raw records (relay is dumb — just bytes)
raw = relay_ev.handle_op({"op": "read_events", "max_events": 8})
assert "events" in raw and raw["count"] > 0, f"expected events, got: {raw}"
print(f"\n  relay read_events  → {raw['count']} raw records (hex)")

# decode_events interprets them (guardian is the brain)
dec = g_ev.decode_events()
assert "error" not in dec, f"decode_events failed: {dec}"
assert dec["count"] > 0
print(f"  guardian decode_events → {dec['count']} actions: {dec['actions']}")
assert any("button" in a or "axis" in a or "sync" in a for a in dec["actions"])
print("CONTROL INPUT WORKS WITH CONSENT")


# ─────────────────────────────────────────────────────────────────────────────
# INPUT_EVENT — TEST D3: SYSTEM-KEYBOARD PATH flagged EXTRA SENSITIVE
# ─────────────────────────────────────────────────────────────────────────────
section("INPUT_EVENT — TEST D3: system keyboard/mouse path flagged EXTRA sensitive")

print("  is_system_input() is a pure path-pattern heuristic (no stat/read).")
print("  event0..event3 → primary keyboard/mouse on most Linux desktops.")
print("  This triggers EXTRA SENSITIVE marking even when the device is listed.")

for ev_num, expected_sys in [(0, True), (1, True), (5, False), (15, False)]:
    fake_path = f"/dev/input/event{ev_num}"
    result = is_system_input(fake_path)
    assert result == expected_sys, f"is_system_input({fake_path}) expected {expected_sys}, got {result}"
    marker = "EXTRA SENSITIVE" if result else "peripheral (ok)"
    print(f"  is_system_input('/dev/input/event{ev_num}') = {result}  → {marker}")

# Use system_input_override to simulate a relay pointed at a keyboard-like path
relay_syskey = DumbRelay("relay-syskey", ev_path,
                         kind_override="input_event",
                         consent_granted=False,
                         system_input_override=True)
cap_syskey = relay_syskey.capabilities()[0]
assert cap_syskey["system_input"] is True
assert cap_syskey["access"] == "consent_required"
show("system_input", cap_syskey["system_input"])
show("access",       cap_syskey["access"])
print("\nSYSTEM KEYBOARD FLAGGED SENSITIVE")


# ─────────────────────────────────────────────────────────────────────────────
# TEST E — RAW_GENERIC: unrecognised device handled gracefully
# ─────────────────────────────────────────────────────────────────────────────
section("TEST E — RAW_GENERIC: unrecognised device falls back to generic byte interface")

raw_path = _make_raw_generic()
all_temps.append(raw_path)
print(f"  [sim] binary file = {raw_path}  (real host: any unrecognised /dev node)")
print("  detect_kind() returns raw_generic → relay never crashes, offers read/write")

relay_raw = DumbRelay("relay-raw", raw_path)   # no override — auto-detects raw_generic
cap_raw   = relay_raw.capabilities()[0]
assert cap_raw["kind"] == "raw_generic", f"expected raw_generic, got {cap_raw['kind']}"
print(f"\nRELAY KIND=raw_generic EXPOSES {cap_raw['primitives']}")

# TEST A: only read_bytes / write_bytes offered
assert set(cap_raw["primitives"]) == {"read_bytes", "write_bytes"}
bad = relay_raw.handle_op({"op": "list_entries", "path": ""})
assert "error" in bad
print(f"  reject list_entries → {bad['error']}")

# TEST B: guardian hexdump from those primitives
g_raw = GuardianAgent("guardian-raw")
g_raw.attach(cap_raw)
assert g_raw.skills == ["hexdump", "capture"]

hex_r = g_raw.hexdump(length=32)
assert "hexdump" in hex_r and len(hex_r["hexdump"]) > 0
print(f"  hexdump(32 bytes):\n")
for line in hex_r["hexdump"].splitlines():
    print(f"    {line}")
print("GUARDIAN MADE raw_generic SMART")

# TEST C: VSO
vso_raw = VirtualSmartObject(cap_raw, g_raw)
adv_raw = vso_raw.advertised_capability()
assert adv_raw["name"] == "smart_raw_device"
assert "hexdump" in adv_raw["tags"]
cap_r = vso_raw.handle_request({"action": "capture", "length": "16"})
assert "bytes_read" in cap_r
print(f"  VSO name={adv_raw['name']}  capture 16 bytes → {cap_r['bytes_read']} bytes")
print("UNKNOWN DUMB DEVICE HANDLED GENERICALLY")


# ─────────────────────────────────────────────────────────────────────────────
# TEST F — SCOPE JAIL for char/sensor/input devices
# ─────────────────────────────────────────────────────────────────────────────
section("TEST F — SCOPE JAIL: char/sensor/input relays reject any other path")

print("  For char_stream / sensor_file / input_event the relay is scoped to the")
print("  EXACT device path. Any op carrying a different 'path' is rejected.")

# char_stream jail
relay_j = DumbRelay("relay-jail-char", uart_path, kind_override="char_stream")
for bad_path in ["/etc/passwd", uart_path + "_other", "/tmp/escaped"]:
    r = relay_j.handle_op({"op": "read_stream", "path": bad_path, "max_bytes": 64})
    assert r.get("error") == "path_sandbox_violation", \
        f"char_stream scope jail failed for '{bad_path}': {r}"
    print(f"  char_stream op(path={bad_path!r}) → {r['error']}")

# sensor_file jail
relay_js = DumbRelay("relay-jail-sensor", sensor_path, kind_override="sensor_file")
r = relay_js.handle_op({"op": "read_value", "path": "/etc/hostname"})
assert r.get("error") == "path_sandbox_violation", \
    f"sensor_file scope jail failed: {r}"
print(f"  sensor_file op(path='/etc/hostname') → {r['error']}")

# input_event jail (consent granted so we get to the jail check)
relay_ji = DumbRelay("relay-jail-input", ev_path,
                     kind_override="input_event", consent_granted=True)
r = relay_ji.handle_op({"op": "read_events", "path": "/dev/input/event0"})
assert r.get("error") == "path_sandbox_violation", \
    f"input_event scope jail failed: {r}"
print(f"  input_event op(path='/dev/input/event0') → {r['error']}")

print("\nSCOPE JAIL HELD FOR RAW DEVICE")


# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP
# ─────────────────────────────────────────────────────────────────────────────
for p in all_temps:
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.exists(p):
        os.unlink(p)

print(f"\n{DIVIDER}")
print("  GUARDIAN ANY-DEVICE (CASE 2 GENERALIZED) OK")
print(DIVIDER)
