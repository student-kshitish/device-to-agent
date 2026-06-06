"""
examples/guardian_demo.py — CASE 2: Capability Guardian

Architecture:
    dumb hardware + borrowed brain = virtual smart object
    D2A is the nerve between them

    DumbRelay  (host where device is plugged):
        - no intelligence; drive+relay only
        - exposes raw primitives: list_entries, read_bytes, write_bytes, stat, delete
        - could run on a $5 microcontroller-class host unchanged

    GuardianAgent  (runs anywhere, remote from device):
        - ALL intelligence lives here: index, search, backup, organize
        - builds index from relay primitives; index lives in guardian, not on device

    VirtualSmartObject  (what the network sees):
        - fusion of relay + guardian
        - advertises as "smart_storage" — indexed, searchable, backup_capable
        - routes high-level requests down through guardian → relay → device

Two temp directories stand in for raw peripherals.  On a real host, pass the
actual mount path from resource_probes instead of the temp dir.
"""

import sys
import os
import shutil
import tempfile
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d2a.guardian.relay import DumbRelay
from agents.guardian_agent import GuardianAgent
from d2a.guardian.virtual_object import VirtualSmartObject

DIVIDER = "=" * 70


def section(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP: two temp directories simulate raw peripherals (USB mount, SD card, …)
# On a real host, replace these with the mount path from resource_probes.
# ─────────────────────────────────────────────────────────────────────────────

device1_dir = tempfile.mkdtemp(prefix="d2a_device1_")
device2_dir = tempfile.mkdtemp(prefix="d2a_device2_")

print(f"\n[setup] device 1 (simulates USB/SD mount): {device1_dir}")
print(f"[setup] device 2 (simulates backup target): {device2_dir}")
print("[setup] On a real host these would be paths discovered by resource_probes.")

# Populate device 1 with dummy files (simulate files on a USB stick)
with open(os.path.join(device1_dir, "notes.txt"), "wb") as fh:
    fh.write(b"meeting notes from Q3 review\n")
with open(os.path.join(device1_dir, "photo.jpg"), "wb") as fh:
    fh.write(b"JPEG_SIMULATED_BYTES_FOR_DEMO")
with open(os.path.join(device1_dir, "music.mp3"), "wb") as fh:
    fh.write(b"MP3_SIMULATED_BYTES_FOR_DEMO")

print("[setup] Device 1 files: notes.txt, photo.jpg, music.mp3")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — DUMB RELAY ONLY
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 1 — DUMB RELAY ONLY: prove relay has no intelligence")

print("  Relay is a nerve ending — it drives the device and relays bytes, nothing more.")
print("  It does NOT know what is on the device, cannot search, cannot index.")

relay1 = DumbRelay(node_id="relay-node-01", device_path_or_probe=device1_dir)

# capabilities() — discovered, never hardcoded
caps = relay1.capabilities()
assert caps, "relay must advertise at least one raw capability"
cap_record = caps[0]
print(f"\n  relay1.capabilities()[0]:")
print(f"    name        = '{cap_record['name']}'")
print(f"    kind        = '{cap_record['kind']}'")
print(f"    path        = {cap_record['path']}")
print(f"    size_bytes  = {cap_record['size_bytes']}")
print(f"    writable    = {cap_record['writable']}")
assert cap_record["name"] == "raw_block_fs"
assert cap_record["kind"] == "block_fs"

# list_entries — primitive
r = relay1.handle_op({"op": "list_entries", "path": ""})
assert "entries" in r, f"list_entries failed: {r}"
names = [e["name"] for e in r["entries"]]
print(f"\n  list_entries('') → {len(r['entries'])} entries: {names}")
assert "notes.txt" in names

# read_bytes — primitive
r = relay1.handle_op({"op": "read_bytes", "path": "notes.txt", "offset": 0, "length": 12})
assert "data" in r and "error" not in r, f"read_bytes failed: {r}"
print(f"  read_bytes('notes.txt', 0, 12) → {r['bytes_read']} bytes "
      f"({bytes.fromhex(r['data'])!r})")

# stat — primitive
r = relay1.handle_op({"op": "stat", "path": "notes.txt"})
assert "size" in r and "error" not in r, f"stat failed: {r}"
print(f"  stat('notes.txt') → size={r['size']}  is_file={r['is_file']}")

# prove relay has NO search op — completely brainless
r_bad = relay1.handle_op({"op": "search", "query": "notes"})
assert "error" in r_bad, "relay must NOT support search — it is brainless"
print(f"\n  handle_op(search) → {r_bad}  ← no intelligence in relay, as intended")

print("\n  RELAY IS DUMB (primitives only)")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — GUARDIAN ATTACHES
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 2 — GUARDIAN ATTACHES: borrowed intelligence from outside the hardware")

print("  GuardianAgent attaches to relay1; its skills are built from relay primitives.")
print("  The index lives in the guardian — NOT on the device, NOT in the relay.")

guardian = GuardianAgent(name="guardian-alpha", skills=["index", "search", "backup", "organize"])
bind_result = guardian.attach(cap_record)
print(f"\n  guardian.attach() → {bind_result}")

# index() walks the device via primitives; result lives in guardian._index
index_result = guardian.index()
print(f"\n  guardian.index() →")
print(f"    indexed     = {index_result['indexed']}")
print(f"    index_lives = '{index_result['index_lives']}'  ← NOT on device, NOT in relay")
print(f"    paths       = {index_result['paths']}")
assert index_result["indexed"] == 3
assert index_result["index_lives"] == "guardian"

# search() queries guardian._index — intelligence lives here, not on device
search_result = guardian.search("notes")
print(f"\n  guardian.search('notes') →")
print(f"    query       = '{search_result['query']}'")
print(f"    matches     = {search_result['matches']}")
print(f"    searched_in = '{search_result['searched_in']}'  ← guardian index, not device")
assert search_result["count"] > 0, "expected at least one match for 'notes'"
assert "notes.txt" in search_result["matches"]
assert search_result["searched_in"] == "guardian_index"

print("\n  GUARDIAN GAVE IT A BRAIN")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — VIRTUAL SMART OBJECT
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 3 — VIRTUAL SMART OBJECT: dumb relay + guardian = one smart capability")

vso = VirtualSmartObject(relay_record=cap_record, guardian=guardian)

adv = vso.advertised_capability()
print(f"\n  vso.advertised_capability():")
print(f"    name        = '{adv['name']}'")
print(f"    tags        = {adv['tags']}")
print(f"    backed_by   = {adv['backed_by']}  (dumb relay node)")
print(f"    guardian    = {adv['guardian']}  (borrowed brain)")
print(f"    skills      = {adv['skills']}")
print(f"    live_state  = {adv['live_state']}")
assert adv["name"] == "smart_storage"
assert "searchable" in adv["tags"]
assert "indexed" in adv["tags"]
assert "backup_capable" in adv["tags"]
assert adv["live_state"]["entries_indexed"] == 3

# Another agent sends a high-level request — it never knows the device is dumb
for query in ("photo", "notes", "music"):
    req    = {"action": "search", "query": query}
    result = vso.handle_request(req)
    print(f"\n  [other agent] vso.handle_request({req})")
    print(f"    → matches={result['matches']}  count={result['count']}")
    assert result["count"] > 0, f"VSO search should find '{query}'"

print("\n  DUMB USB NOW APPEARS SMART")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — BACKUP across two relays
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 4 — GUARDIAN BACKUP: two dumb devices, guardian orchestrates the copy")

print("  relay2 = second dumb device (empty temp dir); guardian copies relay1 → relay2.")
print("  Neither device knows about the other; guardian is the sole orchestrator.")

relay2     = DumbRelay(node_id="relay-node-02", device_path_or_probe=device2_dir)
caps2      = relay2.capabilities()
assert caps2, "relay2 must advertise a capability"

# backup() uses only primitives: read from relay1, write to relay2
backup_result = guardian.backup(relay2)
print(f"\n  guardian.backup(relay2) → {backup_result}")
assert backup_result["ok"],      f"backup returned errors: {backup_result['errors']}"
assert backup_result["copied"] == 3, f"expected 3 files copied, got {backup_result['copied']}"

# Verify files actually landed on device 2 (check via relay2 primitives too)
r_list2 = relay2.handle_op({"op": "list_entries", "path": ""})
landed   = sorted(e["name"] for e in r_list2.get("entries", []))
print(f"  Files on device 2 (via relay2.list_entries): {landed}")
assert "notes.txt" in landed, "notes.txt should be on device 2"
assert "photo.jpg" in landed, "photo.jpg should be on device 2"
assert "music.mp3" in landed, "music.mp3 should be on device 2"

# Spot-check content integrity via relay2 primitives
r_read2 = relay2.handle_op({"op": "read_bytes", "path": "notes.txt", "offset": 0, "length": 50})
assert "error" not in r_read2
copied_content = bytes.fromhex(r_read2["data"])
print(f"  notes.txt content on device 2: {copied_content!r}")
assert b"meeting notes" in copied_content, "file content must be preserved"

print("\n  GUARDIAN BACKUP OK")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — PATH SANDBOX
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 5 — PATH SANDBOX: relay rejects any path escaping the device root")

print("  Relay must reject path-traversal attempts with path_sandbox_violation.")
print("  Absolute paths are treated as relative (leading / stripped).")

escape_attempts = [
    "../../etc/passwd",
    "../../../root/.ssh/id_rsa",
    "subdir/../../../../etc/shadow",
]
for bad_path in escape_attempts:
    r = relay1.handle_op({"op": "stat", "path": bad_path})
    print(f"  stat('{bad_path}') → {r}")
    assert "error" in r and r["error"] == "path_sandbox_violation", \
        f"sandbox must reject '{bad_path}', got: {r}"

# read_bytes escape attempt
r_esc = relay1.handle_op({
    "op": "read_bytes", "path": "../../etc/passwd", "offset": 0, "length": 100,
})
assert "error" in r_esc and r_esc["error"] == "path_sandbox_violation"
print(f"  read_bytes('../../etc/passwd', ...) → {r_esc}")

# write_bytes escape attempt
r_esc2 = relay1.handle_op({
    "op": "write_bytes", "path": "../../tmp/injected", "offset": 0, "data": "deadbeef",
})
assert "error" in r_esc2 and r_esc2["error"] == "path_sandbox_violation"
print(f"  write_bytes('../../tmp/injected', ...) → {r_esc2}")

print("\n  RELAY PATH SANDBOX HELD")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — DEVICE-AGNOSTIC
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 6 — DEVICE-AGNOSTIC: relay code contains zero hardcoded device types")

print("  The DumbRelay ran on a plain tmpfs directory.")
print("  It would run IDENTICALLY on:")
print("    - A USB mass storage mount  (e.g. /media/user/USB_DRIVE)")
print("    - An SD card mount          (e.g. /media/user/SD_CARD)")
print("    - Any NFS/CIFS/FUSE share")
print("    - A $5 microcontroller host running this file over D2A transport")
print("    - A phone, Raspberry Pi, laptop — wherever the device is plugged in")

# Assert: relay source has no hardcoded device type strings
import d2a.guardian.relay as relay_module
relay_src = inspect.getsource(relay_module)
forbidden = ["usb", "sd_card", "microcontroller", "/dev/sd", "/media/usb", "raspberry"]
for token in forbidden:
    assert token not in relay_src.lower(), \
        f"relay.py must NOT hardcode device type '{token}' — found in source!"

print("\n  relay.py source verified: zero hardcoded device types.")
print("  Device kind was discovered at runtime via os.stat() mode bits,")
print("  and capability 'name' / 'kind' were set from that probe result.")
print("\n  DEVICE-AGNOSTIC OK")


# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP
# ─────────────────────────────────────────────────────────────────────────────

shutil.rmtree(device1_dir, ignore_errors=True)
shutil.rmtree(device2_dir, ignore_errors=True)

print(f"\n{DIVIDER}")
print("  CAPABILITY GUARDIAN (CASE 2) OK")
print(DIVIDER)
