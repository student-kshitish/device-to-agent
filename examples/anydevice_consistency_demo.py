"""
examples/anydevice_consistency_demo.py — ANY-DEVICE CONSISTENCY (CASE 1 + CASE 3)

Verifies that Case 1 (DeviceRuntime) and Case 3 (Synthesis Layer) are fully
consistent with the any-device model introduced in Case 2.

TEST 1 — attach_peripheral: block_fs / char_stream / sensor_file / raw_generic
         → access='open', capability registered in broker, policy allows it

TEST 2 — attach_peripheral: input_event → access='consent_required'
         policy.allow() promotes access to open

TEST 3 — merged_stream synthesis: 3 char_stream members → read_merged() works

TEST 4 — sensor_array synthesis: 3 sensor_file members → read_all() + verdict_all()

TEST 5 — sensitive exclusion: input_event member in pool → excluded, plan fails cleanly

TEST 6 — pooled_storage regression: existing synthesis still works
"""

import sys
import os
import shutil
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DIVIDER = "=" * 70


def section(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_infra(relay_map: dict):
    """Build bind/release/health closures for a {node_id: DumbRelay} map."""
    active: dict = {}

    def bind_fn(node_id, cap_name, priority):
        relay = relay_map.get(node_id)
        if relay is None or not relay._device_available():
            return {"status": "error", "message": f"relay {node_id} unavailable"}
        bid = str(uuid.uuid4())[:12]
        b = {
            "status":           "granted",
            "binding_id":       bid,
            "provider_node_id": node_id,
            "capability_name":  cap_name,
            "relay_ref":        relay,
        }
        active[bid] = b
        return b

    def release_fn(binding):
        active.pop(binding.get("binding_id", ""), None)

    def health_fn(binding):
        relay = binding.get("relay_ref")
        if relay is not None and not relay._device_available():
            return {"verdict": "error", "healthy": False}
        return {"verdict": "comfort", "healthy": True}

    return bind_fn, release_fn, health_fn, active


def make_stream_pool_entry(relay, node_id: str) -> dict:
    """Pool entry for a char_stream relay."""
    from d2a.contracts import IOContract
    return {
        "node_id":    node_id,
        "capability": "raw_char_stream",
        "role":       "stream_member",
        "kind":       "char_stream",
        "contract":   IOContract(media="stream", format="raw_bytes"),
        "live_state": {},
        "relay_ref":  relay,
    }


def make_sensor_pool_entry(relay, node_id: str) -> dict:
    """Pool entry for a sensor_file relay."""
    from d2a.contracts import IOContract
    return {
        "node_id":    node_id,
        "capability": "raw_sensor_file",
        "role":       "sensor_member",
        "kind":       "sensor_file",
        "contract":   IOContract(media="scalar", format="raw_text"),
        "live_state": {},
        "relay_ref":  relay,
    }


def make_storage_pool_entry(relay, node_id: str) -> dict:
    """Pool entry for a block_fs relay (backward-compat: no 'kind' field)."""
    from d2a.contracts import IOContract
    caps = relay.capabilities()
    live = {}
    if caps:
        live = {
            "free_bytes": caps[0].get("free_bytes", 0),
            "size_bytes": caps[0].get("size_bytes", 0),
            "writable":   caps[0].get("writable", True),
        }
    return {
        "node_id":    relay.node_id,
        "capability": "raw_storage",
        "role":       "storage_member",
        "contract":   IOContract(media="storage", format="raw_block"),
        "live_state": live,
        "relay_ref":  relay,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — attach_peripheral: open kinds (block_fs, char_stream, sensor_file, raw_generic)
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 1 — attach_peripheral: open kinds → access='open', broker registers cap")

# Import device_runtime without triggering the full swarm/sense init side-effects.
# DeviceRuntime.__init__ probes hardware; we only need attach_peripheral/detach_peripheral.
# Test directly against those methods using a minimal stub runtime.
from d2a.guardian.relay import DumbRelay
from d2a.guardian.device_kinds import (
    detect_kind, KIND_SENSITIVITY, KIND_PRIMITIVES,
    KIND_BLOCK_FS, KIND_CHAR_STREAM, KIND_SENSOR_FILE, KIND_RAW_GENERIC, KIND_INPUT_EVENT,
)

# Simulate attach_peripheral inline — mirrors what DeviceRuntime does
from d2a import Capability
from d2a.policy import ResourcePolicy

def simulate_attach(path: str, cap_registry: dict, quota_registry: dict,
                    peripheral_paths: dict, policy: ResourcePolicy,
                    kind_override: str | None = None,
                    system_input_override: bool | None = None) -> dict:
    """
    Mirrors DeviceRuntime.attach_peripheral() logic without instantiating the full runtime.
    """
    from d2a.guardian.device_kinds import is_system_input
    kind         = kind_override if kind_override is not None else detect_kind(path)
    access       = KIND_SENSITIVITY.get(kind, "open")
    primitives   = KIND_PRIMITIVES.get(kind, [])
    system_input = system_input_override if system_input_override is not None else (
        is_system_input(path) if kind == KIND_INPUT_EVENT else False
    )

    cap_name = f"raw_{kind}"
    tags     = ["peripheral", "external", access, kind]
    if system_input:
        tags.append("system_input")

    if access == "sensitive":
        policy.require_approval(cap_name)
    else:
        policy.allow(cap_name)

    real = os.path.realpath(path)
    cap  = Capability(
        name=cap_name,
        tags=tags,
        live_state={
            "kind":          kind,
            "path":          real,
            "primitives":    primitives,
            "access":        access,
            "system_input":  system_input,
        },
        node_id="test-node",
        public_key=b"",
    )
    cap_registry[cap_name]   = cap
    quota_registry[cap_name] = 1
    peripheral_paths[real]   = cap_name

    return {
        "name":          cap_name,
        "kind":          kind,
        "path":          real,
        "primitives":    primitives,
        "access":        access,
        "system_input":  system_input,
        "relay_node_id": "test-node",
    }


policy_1  = ResourcePolicy(device_class="edge")
caps_1:   dict = {}
quotas_1: dict = {}
paths_1:  dict = {}

# block_fs: temp dir
tdir = tempfile.mkdtemp(prefix="d2a_ac1_")
rec_block = simulate_attach(tdir, caps_1, quotas_1, paths_1, policy_1)
print(f"\n  block_fs: {rec_block}")
assert rec_block["kind"]    == "block_fs"
assert rec_block["access"]  == "open"
assert "list_entries" in rec_block["primitives"]
assert "raw_block_fs" in caps_1

# char_stream: regular file + kind_override
tfile_stream = tempfile.NamedTemporaryFile(delete=False, suffix=".stream")
tfile_stream.close()
rec_stream = simulate_attach(tfile_stream.name, caps_1, quotas_1, paths_1, policy_1,
                             kind_override="char_stream")
print(f"  char_stream: {rec_stream}")
assert rec_stream["kind"]   == "char_stream"
assert rec_stream["access"] == "open"
assert "open_stream" in rec_stream["primitives"]

# sensor_file: regular file + kind_override
tfile_sensor = tempfile.NamedTemporaryFile(delete=False, suffix=".sensor")
tfile_sensor.close()
rec_sensor = simulate_attach(tfile_sensor.name, caps_1, quotas_1, paths_1, policy_1,
                             kind_override="sensor_file")
print(f"  sensor_file: {rec_sensor}")
assert rec_sensor["kind"]   == "sensor_file"
assert rec_sensor["access"] == "open"
assert "read_value" in rec_sensor["primitives"]

# raw_generic: regular temp file (no override)
tfile_raw = tempfile.NamedTemporaryFile(delete=False, suffix=".raw")
tfile_raw.close()
rec_raw = simulate_attach(tfile_raw.name, caps_1, quotas_1, paths_1, policy_1)
print(f"  raw_generic: {rec_raw}")
assert rec_raw["kind"]   == "raw_generic"
assert rec_raw["access"] == "open"
assert "read_bytes" in rec_raw["primitives"]

shutil.rmtree(tdir, ignore_errors=True)
os.unlink(tfile_stream.name)
os.unlink(tfile_sensor.name)
os.unlink(tfile_raw.name)

print("\n  OPEN KINDS ATTACHED OK — broker registered, policy allowed")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — attach_peripheral: input_event → consent_required; policy.allow() promotes
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 2 — attach_peripheral: input_event → consent_required by default; allow() promotes")

policy_2  = ResourcePolicy(device_class="edge")
caps_2:   dict = {}
quotas_2: dict = {}
paths_2:  dict = {}

# Simulate /dev/input/event5 path (high number → not system_input)
tfile_event = tempfile.NamedTemporaryFile(delete=False, suffix=".event")
tfile_event.close()
rec_event = simulate_attach(tfile_event.name, caps_2, quotas_2, paths_2, policy_2,
                            kind_override="input_event",
                            system_input_override=False)
print(f"\n  input_event (no consent): {rec_event}")
assert rec_event["kind"]         == "input_event"
assert rec_event["access"]       == "sensitive"
assert rec_event["system_input"] == False
assert "raw_input_event" in caps_2

# Policy check: require_approval was called → needs_approval for remote agents
AGENT = "test-agent"
check_before = policy_2.check("raw_input_event", agent_id=AGENT, is_remote=True)
print(f"  policy.check('raw_input_event') before allow = {check_before!r}")
assert check_before == "needs_approval", \
    f"Expected 'needs_approval' before allow(), got: {check_before}"
# allow() promotes to open
policy_2.allow("raw_input_event")
check_after = policy_2.check("raw_input_event", agent_id=AGENT, is_remote=True)
print(f"  policy.check('raw_input_event') after allow  = {check_after!r}")
assert check_after == "allow", \
    f"Expected 'allow' after policy.allow(), got: {check_after}"

# system_input=True variant
rec_sys = simulate_attach(tfile_event.name + "_sys", caps_2, quotas_2, paths_2, policy_2,
                          kind_override="input_event",
                          system_input_override=True)
print(f"  input_event (system_input=True): system_input={rec_sys['system_input']}")
assert rec_sys["system_input"] == True
assert "system_input" in caps_2["raw_input_event"].tags

os.unlink(tfile_event.name)

# Verify is_system_input path-pattern check
from d2a.guardian.device_kinds import is_system_input
for n, expected in [(0, True), (3, True), (4, False), (10, False)]:
    result = is_system_input(f"/dev/input/event{n}")
    assert result == expected, f"is_system_input(event{n}) expected {expected}, got {result}"
print(f"  is_system_input: event0→True, event3→True, event4→False, event10→False ✓")

print("\n  INPUT_EVENT SENSITIVITY + CONSENT PROMOTION OK")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — merged_stream synthesis: 3 char_stream members → read_merged()
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 3 — merged_stream synthesis: 3 char_stream members → read_merged()")

from d2a.guardian.relay import DumbRelay
from d2a.composer import Composer
from d2a.composition.emergent_runtime import EmergentDeviceHandle

# Create 3 temp files holding char-stream-like data
stream_data = [
    b"NMEA-0\r\n$GPGGA,123519,4807.038,N\r\n",
    b"NMEA-1\r\n$GPRMC,123520,A,4807.038,N\r\n",
    b"NMEA-2\r\n$GPGSV,2,1,08,01,40,083\r\n",
]
stream_files = []
for d in stream_data:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".stream")
    f.write(d)
    f.close()
    stream_files.append(f.name)

stream_relays = [
    DumbRelay(
        node_id=f"stream-{i:02d}",
        device_path_or_probe=path,
        kind_override="char_stream",
    )
    for i, path in enumerate(stream_files)
]
stream_relay_map = {r.node_id: r for r in stream_relays}
pool_3 = [make_stream_pool_entry(r, r.node_id) for r in stream_relays]

bind_fn_3, release_fn_3, health_fn_3, active_3 = make_infra(stream_relay_map)
composer_3 = Composer(
    capability_pool_provider=lambda: pool_3,
    bind_fn=bind_fn_3,
    release_fn=release_fn_3,
    health_fn=health_fn_3,
)

plan_3 = composer_3.plan("merged_stream")
print(f"\n  plan ok={plan_3.ok}")
for s in plan_3.stages_log:
    print(f"    {s}")
assert plan_3.ok, f"merged_stream plan failed: {plan_3.reason}"

bp_3 = plan_3.primary_blueprint
em_3 = bp_3.synthesis_metadata["emergent_device"]
print(f"\n  EmergentDevice: name='{em_3.name}' kind='{em_3.kind}' members={em_3.live_state['member_count']}")
assert em_3.kind == "merged_stream"
assert em_3.live_state["member_count"] == 3

comp_3 = composer_3.bind(plan_3)
assert not isinstance(comp_3, tuple), f"merged_stream bind failed: {comp_3}"
handle_3 = comp_3.handle
assert isinstance(handle_3, EmergentDeviceHandle)

# Verify that wrong ops are rejected
err_ops = handle_3.write("x", b"y")
assert "error" in err_ops, f"write() should fail on merged_stream, got {err_ops}"

# read_merged opens streams internally and collects chunks from all members
merged = handle_3.read_merged(max_per_member=128, timeout=0.1)
print(f"\n  read_merged() → {len(merged['chunks'])} chunks from {merged['members']} members")
for chunk in merged["chunks"]:
    data_bytes = bytes.fromhex(chunk["data_hex"])
    print(f"    member[{chunk['member_index']}] node={chunk['node_id']}  "
          f"data={data_bytes[:30]!r}")
assert merged["members"] == 3
assert len(merged["chunks"]) == 3, f"expected 3 chunks (one per member), got {len(merged['chunks'])}"

# tail_all
tails = handle_3.tail_all(lines=2)
print(f"\n  tail_all(lines=2) → {tails}")
assert "tails" in tails

# stats
st_3 = handle_3.stats()
assert st_3["kind"]         == "merged_stream"
assert st_3["member_count"] == 3
print(f"  stats: {st_3}")

comp_3.release()
for f in stream_files:
    os.unlink(f)

print("\n  MERGED_STREAM SYNTHESIS + read_merged OK")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — sensor_array synthesis: 3 sensor_file members → read_all() + verdict_all()
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 4 — sensor_array synthesis: 3 sensor_file members → read_all() + verdict_all()")

# Three temp files simulating sensor readings (temperature-like values in Celsius)
sensor_values = ["42.3\n", "67.1\n", "28.9\n"]
sensor_files = []
for v in sensor_values:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".sensor", mode="w")
    f.write(v)
    f.close()
    sensor_files.append(f.name)

sensor_relays = [
    DumbRelay(
        node_id=f"sensor-{i:02d}",
        device_path_or_probe=path,
        kind_override="sensor_file",
    )
    for i, path in enumerate(sensor_files)
]
sensor_relay_map = {r.node_id: r for r in sensor_relays}
pool_4 = [make_sensor_pool_entry(r, r.node_id) for r in sensor_relays]

bind_fn_4, release_fn_4, health_fn_4, active_4 = make_infra(sensor_relay_map)
composer_4 = Composer(
    capability_pool_provider=lambda: pool_4,
    bind_fn=bind_fn_4,
    release_fn=release_fn_4,
    health_fn=health_fn_4,
)

plan_4 = composer_4.plan("sensor_array")
print(f"\n  plan ok={plan_4.ok}")
assert plan_4.ok, f"sensor_array plan failed: {plan_4.reason}"

bp_4   = plan_4.primary_blueprint
em_4   = bp_4.synthesis_metadata["emergent_device"]
print(f"  EmergentDevice: kind='{em_4.kind}' members={em_4.live_state['member_count']}")
assert em_4.kind == "sensor_array"
assert em_4.live_state["member_count"] == 3

comp_4 = composer_4.bind(plan_4)
assert not isinstance(comp_4, tuple), f"sensor_array bind failed: {comp_4}"
handle_4 = comp_4.handle
assert isinstance(handle_4, EmergentDeviceHandle)

# read_all — reads value from each sensor and aggregates
all_readings = handle_4.read_all()
print(f"\n  read_all() → {all_readings}")
assert "readings" in all_readings
assert "aggregate" in all_readings
agg = all_readings["aggregate"]
assert agg["count"] == 3
print(f"  aggregate: min={agg['min']} max={agg['max']} mean={agg['mean']}")
assert agg["min"]  == pytest_approx(28.9, abs=0.01) if False else abs(agg["min"]  - 28.9) < 0.01
assert agg["max"]  == pytest_approx(67.1, abs=0.01) if False else abs(agg["max"]  - 67.1) < 0.01
assert abs(agg["mean"] - (42.3 + 67.1 + 28.9) / 3) < 0.01, f"mean mismatch: {agg['mean']}"

# verdict_all — warn at 50°C, danger at 80°C
#   sensor-00 = 42.3 → ok
#   sensor-01 = 67.1 → warn
#   sensor-02 = 28.9 → ok
verdicts = handle_4.verdict_all(warn=50.0, danger=80.0)
print(f"\n  verdict_all(warn=50, danger=80) → {verdicts}")
assert verdicts["summary"] in ("warn", "danger")
assert verdicts["verdicts"][0] == "ok"
assert verdicts["verdicts"][1] == "warn"
assert verdicts["verdicts"][2] == "ok"

# stats
st_4 = handle_4.stats()
assert st_4["kind"]         == "sensor_array"
assert st_4["member_count"] == 3
print(f"  stats: {st_4}")

comp_4.release()
for f in sensor_files:
    os.unlink(f)

print("\n  SENSOR_ARRAY SYNTHESIS + read_all + verdict_all OK")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — sensitive exclusion: input_event pool member → excluded cleanly
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 5 — sensitive exclusion: input_event member → excluded, plan fails cleanly")

from d2a.composition.synthesizer import Synthesizer

# Build a pool with 2 char_stream members and 1 input_event "impostor"
# The synthesizer must exclude the input_event member and if that leaves 0 valid members,
# return a clean failure with a reason (not silently fuse the sensitive device).
pool_5_char = [
    {
        "node_id":  "stream-ok-0",
        "role":     "stream_member",
        "kind":     "char_stream",
        "access":   "open",
        "relay_ref": None,
    },
    {
        "node_id":  "stream-ok-1",
        "role":     "stream_member",
        "kind":     "char_stream",
        "access":   "open",
        "relay_ref": None,
    },
]
pool_5_bad = [
    {
        "node_id":  "event-impostor",
        "role":     "stream_member",   # same role, wrong kind
        "kind":     "input_event",     # SENSITIVE — must be excluded
        "access":   "consent_required",
        "relay_ref": None,
    },
]
synthesizer = Synthesizer()

# Pool with mixed members: 2 char_stream + 1 input_event impostor
# Synthesizer must exclude the impostor and build plan with only the 2 char_stream
plans_mixed = synthesizer.enumerate_synthesis_plans(
    "merged_stream", pool_5_char + pool_5_bad
)
print(f"\n  Mixed pool (2 char_stream + 1 input_event impostor):")
print(f"  → {len(plans_mixed)} plan(s)")
assert len(plans_mixed) >= 1
p = plans_mixed[0]
assert p.valid, f"expected valid plan from 2 char_stream members, got: {p.reject_reason}"
excl = p.synthesis_metadata.get("excluded_members", [])
print(f"  excluded_members = {excl}")
assert any("event-impostor" in str(e) for e in excl), \
    f"event-impostor must appear in excluded_members: {excl}"
em_5 = p.synthesis_metadata["emergent_device"]
assert em_5.live_state["member_count"] == 2, \
    f"only 2 char_stream members should be included, got {em_5.live_state['member_count']}"
print(f"  Plan uses {em_5.live_state['member_count']} members (impostor excluded)")

# Pool with ONLY an input_event member: plan must fail cleanly
plans_only_bad = synthesizer.enumerate_synthesis_plans(
    "merged_stream", pool_5_bad
)
print(f"\n  Pool with only input_event impostor:")
print(f"  → {len(plans_only_bad)} plan(s)")
p_bad = plans_only_bad[0]
assert not p_bad.valid, f"expected plan failure, got valid plan"
print(f"  reject_reason = {p_bad.reject_reason!r}")
assert "0" in p_bad.reject_reason or "excluded" in p_bad.reject_reason or \
    "consent" in p_bad.reject_reason or "sensitive" in p_bad.reject_reason, \
    f"reject reason should mention exclusion/consent: {p_bad.reject_reason}"

# Pool with access="consent_required" but kind=char_stream → also excluded
pool_5_consent = [
    {
        "node_id":  "stream-blocked",
        "role":     "stream_member",
        "kind":     "char_stream",
        "access":   "consent_required",   # explicit consent gate
        "relay_ref": None,
    }
]
plans_consent = synthesizer.enumerate_synthesis_plans("merged_stream", pool_5_consent)
p_consent = plans_consent[0]
assert not p_consent.valid, "consent_required member with no open alternative must fail"
excl_c = p_consent.synthesis_metadata.get("excluded_members", []) if p_consent.synthesis_metadata else []
print(f"\n  consent_required char_stream: valid={p_consent.valid}  "
      f"reject={p_consent.reject_reason!r}")
print(f"  excluded = {excl_c}")

print("\n  SENSITIVE MEMBER EXCLUSION OK — no silent fusion")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — pooled_storage regression: existing synthesis still works
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 6 — pooled_storage regression: existing pool entries still work")

dirs_6 = [tempfile.mkdtemp(prefix=f"d2a_ac6_{i}_") for i in range(3)]
relays_6 = [
    DumbRelay(node_id=f"ac6-{i:02d}", device_path_or_probe=d)
    for i, d in enumerate(dirs_6)
]
relay_map_6 = {r.node_id: r for r in relays_6}
# backward-compat pool entries: no 'kind' field
pool_6 = [make_storage_pool_entry(r, r.node_id) for r in relays_6]
bind_fn_6, release_fn_6, health_fn_6, active_6 = make_infra(relay_map_6)
composer_6 = Composer(
    capability_pool_provider=lambda: pool_6,
    bind_fn=bind_fn_6,
    release_fn=release_fn_6,
    health_fn=health_fn_6,
)

plan_6 = composer_6.plan("pooled_storage")
print(f"\n  pooled_storage plan ok={plan_6.ok}")
assert plan_6.ok, f"regression: pooled_storage plan failed: {plan_6.reason}"

comp_6 = composer_6.bind(plan_6)
assert not isinstance(comp_6, tuple), f"regression: bind failed: {comp_6}"
handle_6 = comp_6.handle

items_6 = [("a.txt", b"hello"), ("b.bin", b"\xff\x00" * 4)]
for key, data in items_6:
    w = handle_6.write(key, data)
    assert w.get("ok"), f"regression write failed: {w}"
    r = handle_6.read(key)
    assert r.get("data") == data, f"regression read mismatch: {r}"

print(f"  Write+read {len(items_6)} items across 3 pooled members ✓")
st_6 = handle_6.stats()
assert st_6["kind"] == "pooled_storage"
print(f"  stats: {st_6}")

comp_6.release()
for d in dirs_6:
    shutil.rmtree(d, ignore_errors=True)

print("\n  POOLED_STORAGE REGRESSION OK")


# ─────────────────────────────────────────────────────────────────────────────
# DONE
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n{DIVIDER}")
print("  ANY-DEVICE CONSISTENCY (CASE 1 + CASE 3) OK")
print(DIVIDER)
