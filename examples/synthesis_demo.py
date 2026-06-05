"""
examples/synthesis_demo.py — CASE 3: Synthesis Layer

CORE PRINCIPLE: the parts become NEURONS of one body; the BRAIN is always
the agent.  Synthesis does NOT create intelligence — it fuses passive parts
into one addressable virtual resource.  The agent remains the single mind.

Architecture:
    Synthesizer (plan-phase sub-stage)
      → builds EmergentDevice blueprint from scattered dumb members
      → flows into standard FallbackPlanner → CompositionPlan
    AtomicBinder (stage 8) binds all members all-or-nothing
    EmergentDeviceHandle — the unified interface the agent drives
    ReleaseManager (stage 10) frees all members together

Scattered members are simulated with temp dirs + DumbRelays (Case 2).
On a real host, pass the actual mount paths from resource_probes instead.
"""

import sys
import os
import shutil
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d2a.guardian.relay import DumbRelay
from d2a.composer import Composer, CompositionPlan
from d2a.composition.emergent_runtime import EmergentDeviceHandle

DIVIDER = "=" * 70


def section(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS: simulated bind/release/health infrastructure
# ─────────────────────────────────────────────────────────────────────────────

def make_infra(relay_map: dict):
    """
    Build bind_fn/release_fn/health_fn closures over a {node_id: DumbRelay} map.
    relay_ref is injected into every binding dict so EmergentDeviceHandle can
    find the relay for each member without a broker round-trip.
    """
    active: dict = {}   # binding_id → binding dict (verify release)

    def bind_fn(node_id, cap_name, priority):
        relay = relay_map.get(node_id)
        if relay is None or not relay._device_available():
            return {"status": "error", "message": f"relay {node_id} unavailable"}
        binding_id = str(uuid.uuid4())[:12]
        b = {
            "status":            "granted",
            "binding_id":        binding_id,
            "provider_node_id":  node_id,
            "capability_name":   cap_name,
            "relay_ref":         relay,
        }
        active[binding_id] = b
        return b

    def release_fn(binding):
        bid = binding.get("binding_id", "")
        active.pop(bid, None)

    def health_fn(binding):
        relay = binding.get("relay_ref")
        if relay is not None and not relay._device_available():
            return {"verdict": "error", "healthy": False}
        return {"verdict": "comfort", "healthy": True}

    return bind_fn, release_fn, health_fn, active


def make_pool_entry(relay: DumbRelay, role: str) -> dict:
    """Build a capability pool entry from a relay's live capabilities."""
    from d2a.contracts import IOContract
    caps = relay.capabilities()
    live = {}
    if caps:
        live = {
            "free_bytes":  caps[0].get("free_bytes", 0),
            "size_bytes":  caps[0].get("size_bytes", 0),
            "writable":    caps[0].get("writable", True),
        }
    return {
        "node_id":      relay.node_id,
        "capability":   "raw_storage",
        "role":         role,
        "contract":     IOContract(media="storage", format="raw_block"),
        "device_class": "storage",
        "live_state":   live,
        "relay_ref":    relay,
    }


def make_composer(pool: list, relay_map: dict):
    bind_fn, release_fn, health_fn, active = make_infra(relay_map)
    composer = Composer(
        capability_pool_provider=lambda: pool,
        bind_fn=bind_fn,
        release_fn=release_fn,
        health_fn=health_fn,
    )
    return composer, active


# ─────────────────────────────────────────────────────────────────────────────
# SETUP: 5 temp dirs simulate scattered raw peripherals
# (on a real host, pass actual mount paths from resource_probes)
# ─────────────────────────────────────────────────────────────────────────────

dirs = [tempfile.mkdtemp(prefix=f"d2a_synth_{i}_") for i in range(5)]
# dirs[0..2] → pooled storage members
# dirs[3]   → fast tier (small, simulates RAM-backed or SSD)
# dirs[4]   → slow tier (large, simulates bulk storage)
print(f"\n[setup] 5 temp dirs simulate scattered raw devices:")
for i, d in enumerate(dirs):
    label = ["pooled_0", "pooled_1", "pooled_2", "fast_tier", "slow_tier"][i]
    print(f"  {label}: {d}")
print("[setup] On a real host these are mount paths from resource_probes.")

relays = [DumbRelay(node_id=f"member-{i:02d}", device_path_or_probe=d) for i, d in enumerate(dirs)]
relay_map_all = {r.node_id: r for r in relays}


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — POOLED STORAGE
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 1 — POOLED STORAGE: 3 dumb parts fused into 1 virtual storage pool")

pool_pooled = [make_pool_entry(relays[i], "storage_member") for i in range(3)]
relay_map_p = {relays[i].node_id: relays[i] for i in range(3)}
composer_p, active_p = make_composer(pool_pooled, relay_map_p)

plan_p = composer_p.plan("pooled_storage")
print(f"\n  plan ok={plan_p.ok}")
for s in plan_p.stages_log:
    print(f"    {s}")
assert plan_p.ok, f"plan failed: {plan_p.reason}"

primary_bp = plan_p.primary_blueprint
emergent_p = primary_bp.synthesis_metadata["emergent_device"]
print(f"\n  EmergentDevice:")
print(f"    name          = '{emergent_p.name}'")
print(f"    kind          = '{emergent_p.kind}'")
print(f"    member_count  = {emergent_p.live_state['member_count']}")
print(f"    total_bytes   = {emergent_p.combined_contract['total_bytes']}")
assert emergent_p.combined_contract["total_bytes"] > 0

print(f"\n  placement_map (virtual → real member):")
total_claimed = 0
for i, slot in emergent_p.placement_map.items():
    lo, hi = slot["byte_range"]
    print(f"    member[{i}] node={slot['node_id']}  range=({lo}, {hi})")
    total_claimed += (hi - lo)

assert total_claimed == emergent_p.combined_contract["total_bytes"], \
    "placement_map ranges must sum to total_bytes"

# Bind — atomic all-or-nothing
comp_p = composer_p.bind(plan_p)
assert not isinstance(comp_p, tuple), f"bind failed: {comp_p}"
assert hasattr(comp_p, "handle"), "composition must have an EmergentDeviceHandle"
handle_p = comp_p.handle
print(f"\n  Bound {len(comp_p.bindings)} members atomically")

# Write data across members via the unified handle
items = [
    ("readme.txt",  b"Hello from the virtual pool\n"),
    ("data.bin",    b"\x00\x01\x02\x03" * 16),
    ("config.json", b'{"synth": true}\n'),
]
for key, data in items:
    w = handle_p.write(key, data)
    assert w.get("ok"), f"write failed: {w}"
    print(f"  write('{key}', {len(data)}B) → member {w['placed_on']}  node={w['node_id'][:12]}")

# Read back every item
for key, expected in items:
    r = handle_p.read(key)
    assert r.get("data") == expected, f"read mismatch for {key}: {r}"
    print(f"  read('{key}') → {r['data']!r:.40}  from_member={r['from_member']}")

st = handle_p.stats()
print(f"\n  stats: {st}")
assert st["files_stored"] == 3

print("\n  3 DUMB PARTS = 1 POOLED STORAGE")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — TIERED MEMORY
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 2 — TIERED MEMORY: fast+slow members fused into one hot/cold store")

pool_tiered = [
    make_pool_entry(relays[3], "fast_tier"),
    make_pool_entry(relays[4], "slow_tier"),
]
relay_map_t = {relays[3].node_id: relays[3], relays[4].node_id: relays[4]}
composer_t, active_t = make_composer(pool_tiered, relay_map_t)

plan_t = composer_t.plan("tiered_memory")
print(f"\n  plan ok={plan_t.ok}")
for s in plan_t.stages_log:
    print(f"    {s}")
assert plan_t.ok, f"tiered plan failed: {plan_t.reason}"

emergent_t = plan_t.primary_blueprint.synthesis_metadata["emergent_device"]
print(f"\n  EmergentDevice: kind='{emergent_t.kind}'")
print(f"    fast tier → node {emergent_t.placement_map['fast']['node_id']}")
print(f"    slow tier → node {emergent_t.placement_map['slow']['node_id']}")
print(f"    hot_policy = '{emergent_t.live_state['hot_policy']}'")
print(f"    fast_max   = {emergent_t.live_state['fast_max']}")

comp_t = composer_t.bind(plan_t)
assert not isinstance(comp_t, tuple), f"tiered bind failed: {comp_t}"
handle_t = comp_t.handle
assert isinstance(handle_t, EmergentDeviceHandle)

# Fill fast tier beyond FAST_MAX_ENTRIES to trigger LRU eviction to slow tier
from d2a.composition.synthesis_types import TIERED_FAST_MAX
OVERFLOW = 3  # items beyond capacity
total_items = TIERED_FAST_MAX + OVERFLOW
for i in range(total_items):
    r = handle_t.put(f"item_{i:03d}", f"value_{i}".encode())
    assert r.get("ok"), f"put failed: {r}"

print(f"\n  Put {total_items} items into tiered handle (fast_max={TIERED_FAST_MAX})")
st_t = handle_t.stats()
print(f"  Fast tier: {st_t['fast_entries']}/{TIERED_FAST_MAX} entries — "
      f"{st_t['fast_keys']}")
assert st_t["fast_entries"] == TIERED_FAST_MAX, \
    "fast tier should be full (≤ FAST_MAX_ENTRIES)"

# Evicted items (the first OVERFLOW ones) must be retrievable from slow tier
for i in range(OVERFLOW):
    r = handle_t.get(f"item_{i:03d}")
    assert "error" not in r, f"evicted item not found: item_{i:03d}: {r}"
    assert r["tier"] == "slow", f"expected slow tier, got {r['tier']}"
    assert r["data"] == f"value_{i}".encode()
    print(f"  get('item_{i:03d}') → tier={r['tier']}  data={r['data']!r}")

# Hot items (last FAST_MAX_ENTRIES) must still be in fast tier
for i in range(OVERFLOW, total_items):
    r = handle_t.get(f"item_{i:03d}")
    assert r["tier"] == "fast", f"expected fast tier for item_{i:03d}, got {r['tier']}"

print(f"  Items {OVERFLOW}..{total_items-1}: all in fast tier ✓")
print("\n  TIERED MEMORY EMERGENT DEVICE OK")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — BRAIN IS THE AGENT
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 3 — BRAIN IS THE AGENT: EmergentDevice has no logic of its own")

from d2a.composition.synthesis_types import EmergentDevice
import inspect

# EmergentDevice is a pure dataclass — no methods beyond __init__/__repr__
ed_methods = [
    name for name, _ in inspect.getmembers(EmergentDevice, predicate=inspect.isfunction)
    if not name.startswith("_")
]
print(f"\n  EmergentDevice public methods: {ed_methods}")
assert ed_methods == [], \
    f"EmergentDevice must have NO logic methods; found: {ed_methods}"

print("  EmergentDevice carries DATA ONLY — no routing, no intelligence.")
print("  All routing decisions came from:")
print("    1. Synthesizer (plan-phase rules, inspectable, no ML)")
print("    2. EmergentDeviceHandle (executes the plan against relay primitives)")
print("    3. The AGENT calling handle.write/read/put/get")
print("  The parts (relays) stayed dumb — they executed only byte-level primitives.")

print("\n  PARTS ARE NEURONS, AGENT IS BRAIN")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — ATOMIC BIND FAILURE + FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 4 — ATOMIC + FALLBACK: member bind failure → rollback or degraded plan")

# Make one relay (member-01) unavailable by removing its temp dir
dirs_4 = [tempfile.mkdtemp(prefix="d2a_t4_") for _ in range(3)]
relays_4 = [DumbRelay(node_id=f"t4-{i}", device_path_or_probe=d) for i, d in enumerate(dirs_4)]
relay_map_4 = {r.node_id: r for r in relays_4}

pool_4 = [make_pool_entry(r, "storage_member") for r in relays_4]

# Remove the middle relay's dir AFTER pool entry is built (so capabilities exist in pool)
# but BEFORE bind (so bind_fn returns error)
shutil.rmtree(dirs_4[1], ignore_errors=True)
print(f"\n  [setup] 3 storage members; member t4-1 dir removed → bind will fail for it")

plan_4 = Composer(
    capability_pool_provider=lambda: pool_4,
    bind_fn=make_infra(relay_map_4)[0],
    release_fn=make_infra(relay_map_4)[1],
    health_fn=make_infra(relay_map_4)[2],
).plan("pooled_storage")
assert plan_4.ok, f"plan should succeed (plan-phase uses static pool, not live check): {plan_4.reason}"

# Bind with full set fails (member t4-1 unavailable) → fallback to 2-member subset
bind_fn_4, release_fn_4, health_fn_4, active_4 = make_infra(relay_map_4)
c4 = Composer(
    capability_pool_provider=lambda: pool_4,
    bind_fn=bind_fn_4,
    release_fn=release_fn_4,
    health_fn=health_fn_4,
).bind(plan_4)

if isinstance(c4, tuple) and c4[0] is False:
    # All blueprints failed — complete rollback
    print(f"  Full rollback: {c4[1][:80]}")
    assert not active_4, f"LEAK: {len(active_4)} bindings still active after rollback!"
    print("  Zero bindings leaked after rollback ✓")
else:
    # Fell back to a degraded (N-1 member) plan that succeeded
    print(f"  Fell back to degraded plan: {len(c4.bindings)} members bound")
    assert len(c4.bindings) < 3, "fallback should have fewer members than the full 3"
    c4.release()

shutil.rmtree(dirs_4[0], ignore_errors=True)
shutil.rmtree(dirs_4[2], ignore_errors=True)

print("\n  SYNTHESIS ATOMIC/FALLBACK OK")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — MEMBER DIES MID-USE
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 5 — MEMBER DIES MID-USE: monitor flags it, reports honestly")

# Use 2 pooled members; remove one after binding
dirs_5 = [tempfile.mkdtemp(prefix="d2a_t5_") for _ in range(2)]
relays_5 = [DumbRelay(node_id=f"t5-{i}", device_path_or_probe=d) for i, d in enumerate(dirs_5)]
relay_map_5 = {r.node_id: r for r in relays_5}
pool_5 = [make_pool_entry(r, "storage_member") for r in relays_5]
bind_fn_5, release_fn_5, health_fn_5, active_5 = make_infra(relay_map_5)

comp_5 = Composer(
    capability_pool_provider=lambda: pool_5,
    bind_fn=bind_fn_5, release_fn=release_fn_5, health_fn=health_fn_5,
).bind(
    Composer(
        capability_pool_provider=lambda: pool_5,
        bind_fn=bind_fn_5, release_fn=release_fn_5, health_fn=health_fn_5,
    ).plan("pooled_storage")
)
assert not isinstance(comp_5, tuple), f"t5 bind failed: {comp_5}"

# Write while healthy
w5 = comp_5.handle.write("alive.txt", b"data while healthy")
assert w5.get("ok"), f"write failed: {w5}"
print(f"\n  Wrote 'alive.txt' to member {w5['placed_on']} while healthy")

# Simulate member t5-0 dying: remove its directory
dead_dir = dirs_5[0]
shutil.rmtree(dead_dir, ignore_errors=True)
print(f"  [simulated] member t5-0 directory removed (device unplugged)")

health = comp_5.check_health()
print(f"\n  check_health() → overall_healthy={health['overall_healthy']}")
for k, v in health.items():
    if k != "overall_healthy":
        print(f"    {k}: verdict={v['verdict']}  healthy={v['healthy']}")

assert not health["overall_healthy"], \
    "health check must report unhealthy after member loss"

# Mark the dead member in the handle so subsequent reads are honest
for b in comp_5.bindings:
    relay = b.get("relay_ref")
    if relay and not relay._device_available():
        comp_5.handle._degraded.add(b["provider_node_id"])

# Write to surviving member still works
w5b = comp_5.handle.write("survivor.txt", b"data after member death")
if w5b.get("ok"):
    print(f"  Write to surviving member: ok (placed on member {w5b['placed_on']})")
else:
    print(f"  Write blocked — no surviving member: {w5b}")

# Read from dead member is honest about failure
r5_dead = comp_5.handle.read("alive.txt")
if r5_dead.get("data"):
    print(f"  Read from surviving member: ok")
elif "error" in r5_dead:
    print(f"  Read from dead member → {r5_dead}  (honest about loss)")

comp_5.release()
shutil.rmtree(dirs_5[1], ignore_errors=True)

print("\n  MEMBER LOSS HANDLED HONESTLY")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — RELEASE
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 6 — RELEASE: all members freed atomically")

dirs_6 = [tempfile.mkdtemp(prefix="d2a_t6_") for _ in range(3)]
relays_6 = [DumbRelay(node_id=f"t6-{i}", device_path_or_probe=d) for i, d in enumerate(dirs_6)]
relay_map_6 = {r.node_id: r for r in relays_6}
pool_6 = [make_pool_entry(r, "storage_member") for r in relays_6]
bind_fn_6, release_fn_6, health_fn_6, active_6 = make_infra(relay_map_6)

comp_6 = Composer(
    capability_pool_provider=lambda: pool_6,
    bind_fn=bind_fn_6, release_fn=release_fn_6, health_fn=health_fn_6,
).bind(
    Composer(
        capability_pool_provider=lambda: pool_6,
        bind_fn=bind_fn_6, release_fn=release_fn_6, health_fn=health_fn_6,
    ).plan("pooled_storage")
)
assert not isinstance(comp_6, tuple)

n_bound = len(comp_6.bindings)
print(f"\n  Bound {n_bound} members; active_binding_ids={list(active_6.keys())}")
assert len(active_6) == n_bound

# Release via context manager
with comp_6:
    pass  # exits immediately → __exit__ calls release()

print(f"  After context exit: active_bindings={len(active_6)}  released={comp_6._released}")
assert comp_6._released, "Composition._released must be True after context exit"
assert len(active_6) == 0, f"LEAK: {len(active_6)} bindings still active!"
assert comp_6.bindings == [], "composition.bindings must be empty after release"

for d in dirs_6:
    shutil.rmtree(d, ignore_errors=True)

print("\n  ALL MEMBERS RELEASED")


# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP main test dirs
# ─────────────────────────────────────────────────────────────────────────────

for d in dirs:
    shutil.rmtree(d, ignore_errors=True)

# Release test 1 and test 2 compositions
comp_p.release()
comp_t.release()

print(f"\n{DIVIDER}")
print("  SYNTHESIS (CASE 3) OK")
print(DIVIDER)
