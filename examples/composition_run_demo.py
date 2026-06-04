"""
Capability Composition — Run Phase demo (Part 2 of 2).
Single-process, REAL DeviceRuntime brokers — binds are real, not stubs.
5 tests: happy path, atomic rollback, fallback-on-bind, monitor+runtime-fallback, release.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtimes.device_runtime import DeviceRuntime
from d2a.contracts import IOContract
from d2a.composer import Composer, CompositionPlan

DIVIDER = "=" * 70


def section(title: str):
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP: real DeviceRuntime instances (hardware probed, capability overridden)
# ─────────────────────────────────────────────────────────────────────────────

print("\nSpinning up 4 real DeviceRuntime brokers …")
drone_cam_rt = DeviceRuntime(name="drone-cam", capability_override=["camera"])
pi_cam_rt    = DeviceRuntime(name="pi-cam",    capability_override=["camera"])
gpu_box_1_rt = DeviceRuntime(name="gpu-box-1", capability_override=["gpu"])
gpu_box_2_rt = DeviceRuntime(name="gpu-box-2", capability_override=["gpu"])

# Map from runtime UUID → label (for human-readable output)
node_to_label = {
    drone_cam_rt.node_id: "drone-cam",
    pi_cam_rt.node_id:    "pi-cam",
    gpu_box_1_rt.node_id: "gpu-box-1",
    gpu_box_2_rt.node_id: "gpu-box-2",
}
node_to_rt = {
    drone_cam_rt.node_id: drone_cam_rt,
    pi_cam_rt.node_id:    pi_cam_rt,
    gpu_box_1_rt.node_id: gpu_box_1_rt,
    gpu_box_2_rt.node_id: gpu_box_2_rt,
}

# Composer's pool: precise IOContracts matching Part 1 demo
def make_pool():
    return [
        {
            "node_id":      drone_cam_rt.node_id,
            "capability":   "camera",
            "role":         "producer",
            "contract":     IOContract(media="image", format="raw_rgb",
                                       shape=(1280, 720, 3), rate=30.0),
            "device_class": "embedded",
            "live_state": {"sample_temps_c": [38.0], "load1": 0.3,
                           "cpu_count": 4, "mem_used_percent": 30.0},
        },
        {
            "node_id":      pi_cam_rt.node_id,
            "capability":   "camera",
            "role":         "producer",
            "contract":     IOContract(media="image", format="jpeg",
                                       shape=(1920, 1080, 3), rate=15.0),
            "device_class": "embedded",
            "live_state": {"sample_temps_c": [72.0], "load1": 1.8,
                           "cpu_count": 4, "mem_used_percent": 65.0},
        },
        {
            "node_id":      gpu_box_1_rt.node_id,
            "capability":   "gpu",
            "role":         "consumer",
            "contract":     IOContract(media="tensor", format="float32",
                                       shape=(640, 480, 3), rate=None),
            "device_class": "server",
            "live_state": {"sample_temps_c": [45.0], "load1": 0.2,
                           "cpu_count": 16, "mem_used_percent": 20.0},
        },
        {
            "node_id":      gpu_box_2_rt.node_id,
            "capability":   "gpu",
            "role":         "consumer",
            "contract":     IOContract(media="tensor", format="float32",
                                       shape=(640, 480, 3), rate=None),
            "device_class": "server",
            "live_state": {"sample_temps_c": [88.0], "load1": 14.0,
                           "cpu_count": 16, "mem_used_percent": 85.0},
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# WIRING: bind_fn / release_fn / health_fn / data_fn → real brokers
# ─────────────────────────────────────────────────────────────────────────────

AGENT_ID = "demo-agent-01"

# forced_health[node_id] overrides health check (for TEST 4 simulation only)
forced_health: dict = {}


def bind_fn(node_id, cap_name, priority):
    rt = node_to_rt[node_id]
    result = rt.broker_request(AGENT_ID, cap_name, [], priority)
    if result.get("status") == "queued":
        # Cancel the queue entry immediately so no ghost binding can auto-grant later.
        # The AtomicBinder uses fail-fast semantics: queued = busy for our purposes.
        rt.broker.cancel_queue(AGENT_ID, cap_name)
        result["status"] = "busy"
    result["provider_node_id"] = node_id
    result["capability_name"]  = cap_name
    return result


def release_fn(binding):
    node_id  = binding.get("provider_node_id", "")
    cap_name = binding.get("capability_name", "")
    rt = node_to_rt.get(node_id)
    if rt:
        rt.broker_release(AGENT_ID, cap_name)


def health_fn(binding):
    node_id = binding.get("provider_node_id", "")
    # TEST 4: honour injected distress override
    if node_id in forced_health:
        return forced_health[node_id]
    rt = node_to_rt.get(node_id)
    if rt is None:
        return {"verdict": "error", "healthy": False}
    binding_id = binding.get("binding_id", "")
    b = rt.broker.get_binding(binding_id)
    if b is None or b.status != "active":
        return {"verdict": "expired", "healthy": False}
    return {"verdict": "comfort", "healthy": True}


def data_fn(binding):
    node_id  = binding.get("provider_node_id", "")
    cap_name = binding.get("capability_name", "")
    rt = node_to_rt.get(node_id)
    if rt:
        return rt.data.get_reading(cap_name)
    return {}


def make_composer():
    return Composer(
        capability_pool_provider=make_pool,
        bind_fn=bind_fn,
        release_fn=release_fn,
        health_fn=health_fn,
        data_fn=data_fn,
    )


def label(node_id: str) -> str:
    return node_to_label.get(node_id, node_id[:12])


def print_composition(comp, title="Composition"):
    bp = comp.bound_blueprint
    print(f"\n  {title}: goal={comp.plan.goal}  blueprint_cost={bp.total_cost:.3f}")
    for hop, binding in comp.stages():
        chain_str = (
            " → ".join(a.describe() for a in hop.adapter_chain) or "exact"
        )
        print(f"  hop[{hop.role_index}] {hop.role:8s}  "
              f"node={label(hop.node_id):12s}  cap={hop.capability_name}  "
              f"chain={chain_str}")
        print(f"           binding_id={binding.get('binding_id','?')[:12]}  "
              f"status={binding.get('status','?')}")


def print_run_result(result: dict):
    print(f"\n  run() result: ok={result.get('ok')}  "
          f"consumer_confirmed={result.get('consumer_confirmed')}")
    for s in result.get("stages_executed", []):
        role = s["role"]
        n    = label(s["node_id"])
        if role == "producer":
            adapters = s.get("adapters_applied", [])
            print(f"    [producer] {n:12s}  contract_out={s['contract_out']}")
            if adapters:
                print(f"               adapters: {' → '.join(adapters)}")
            print(f"               data pulled: {s.get('frame_pulled')}")
        elif role == "consumer":
            print(f"    [consumer] {n:12s}  contract_received={s['contract_received']}")
            print(f"               consumer_confirmed: {s.get('consumer_confirmed')}")


def broker_free(rt, cap_name) -> bool:
    return len(rt.broker.active_binds.get(cap_name, [])) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — HAPPY PATH
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 1 — HAPPY PATH: plan → bind → run → confirm contract end-to-end")

composer = make_composer()
plan = composer.plan("vision")
print(f"\n  Plan ok={plan.ok}")
for s in plan.stages_log:
    print(f"    {s}")

comp1 = composer.bind(plan)
assert not isinstance(comp1, tuple), f"Expected Composition, got: {comp1}"
print_composition(comp1, "Bound composition")

result1 = composer.run(comp1)
print_run_result(result1)
assert result1["ok"], f"run failed: {result1}"
assert result1["consumer_confirmed"], "consumer did not confirm contract"
print("\n  VISION PIPELINE RAN, CONTRACT HELD END TO END")

comp1.release()
assert broker_free(drone_cam_rt, "camera"), "camera not released"
assert broker_free(gpu_box_1_rt, "gpu"),    "gpu-box-1 not released"
print("  bindings released, slots verified free")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — ATOMIC ROLLBACK
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 2 — ATOMIC ROLLBACK: all GPU slots full → camera bound then rolled back")

# Occupy BOTH gpu boxes so every blueprint's consumer step fails.
# Primary (drone-cam + gpu-box-1): camera binds → gpu-box-1 full → rollback camera.
# All 4 fallbacks also fail (all need a GPU). bind() returns (False, reason).
# Net: ZERO bindings left, all cameras free.
sq1 = gpu_box_1_rt.broker_request("squatter", "gpu", [], priority=5)
sq2 = gpu_box_2_rt.broker_request("squatter", "gpu", [], priority=5)
assert sq1["status"] == "granted" and sq2["status"] == "granted", \
    "squatters failed to pre-occupy gpu boxes"
print(f"\n  [setup] squatter occupies gpu-box-1 and gpu-box-2 (all GPU slots full)")

print("  Attempting bind(plan) — every blueprint will fail at the consumer step …")
composer2 = make_composer()
plan2 = composer2.plan("vision")

result_t2 = composer2.bind(plan2)
assert isinstance(result_t2, tuple) and result_t2[0] is False, \
    f"expected bind failure, got: {result_t2}"
_, reason = result_t2
print(f"  bind returned: (False, '{reason[:80]}…')")

# AtomicBinder rolled back the camera binding from each blueprint attempt
assert broker_free(drone_cam_rt, "camera"), \
    "ROLLBACK FAILED: drone-cam/camera still bound after bind failure!"
assert broker_free(pi_cam_rt, "camera"), \
    "ROLLBACK FAILED: pi-cam/camera still bound after bind failure!"
print("  drone-cam/camera FREE  (rolled back from primary attempt)")
print("  pi-cam/camera    FREE  (rolled back from fallback attempt)")
print("  ATOMIC ROLLBACK OK")

# Clean up squatters
gpu_box_1_rt.broker_release("squatter", "gpu")
gpu_box_2_rt.broker_release("squatter", "gpu")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — FALLBACK ON BIND
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 3 — FALLBACK ON BIND: primary fails, auto-fallback to backup succeeds")

# Re-occupy gpu-box-1 so primary blueprint fails
squatter2 = gpu_box_1_rt.broker_request("squatter2", "gpu", [], priority=5)
assert squatter2["status"] == "granted"
print(f"\n  [setup] squatter pre-occupied gpu-box-1 again")
print(f"  gpu-box-2 free: {broker_free(gpu_box_2_rt, 'gpu')}")

composer3 = make_composer()
plan3 = composer3.plan("vision")

print(f"\n  Attempting bind(plan) — primary will fail, expect auto-fallback …")
comp3 = composer3.bind(plan3)
assert not isinstance(comp3, tuple), f"expected Composition, got: {comp3}"

bound_nodes_3 = [label(h.node_id) for h in comp3.bound_blueprint.hops]
print(f"  Bound blueprint nodes: {bound_nodes_3}")
assert "gpu-box-1" not in bound_nodes_3, \
    "fallback should NOT use gpu-box-1 (it is occupied)"
assert "gpu-box-2" in bound_nodes_3, \
    "fallback should use gpu-box-2"

result3 = composer3.run(comp3)
print_run_result(result3)
assert result3["ok"], f"run failed: {result3}"
print("\n  FELL BACK TO BACKUP, GOAL ACHIEVED")

comp3.release()
gpu_box_1_rt.broker_release("squatter2", "gpu")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — MONITOR + FALLBACK AT RUNTIME
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 4 — RUNTIME FALLBACK: bound gpu goes distress, re-bind on the fly")

composer4 = make_composer()
plan4 = composer4.plan("vision")
comp4 = composer4.bind(plan4)
assert not isinstance(comp4, tuple), f"bind failed: {comp4}"

bound_gpu_id = next(
    h.node_id for h in comp4.bound_blueprint.hops if h.role == "consumer"
)
print(f"\n  Bound to: {[label(h.node_id) for h in comp4.bound_blueprint.hops]}")
print(f"  Primary consumer: {label(bound_gpu_id)}")

# Simulate: inject distress for the bound GPU (clearly a demo-only override)
forced_health[bound_gpu_id] = {"verdict": "distress", "healthy": False}
print(f"\n  [SIMULATED DISTRESS] {label(bound_gpu_id)} health overridden → distress")

health_before = comp4.check_health()
print(f"  monitor.check() → overall_healthy={health_before['overall_healthy']}")
for k, v in health_before.items():
    if k != "overall_healthy":
        print(f"    {k}: {v}")
assert not health_before["overall_healthy"], "expected unhealthy"

# run() detects unhealthy → auto-rebinds to next fallback → runs successfully
print(f"\n  Calling run() — should detect distress and re-bind to fallback …")
remaining_before = len(comp4._remaining_fallbacks)
result4 = composer4.run(comp4)

# Clear the forced distress AFTER re-bind (new blueprint doesn't include old GPU)
del forced_health[bound_gpu_id]

new_nodes = [label(h.node_id) for h in comp4.bound_blueprint.hops]
print(f"  Re-bound blueprint: {new_nodes}")
assert label(bound_gpu_id) not in new_nodes, \
    "should have re-bound away from the distressed GPU"
print_run_result(result4)
assert result4["ok"], f"run after fallback failed: {result4}"
print("\n  RUNTIME FALLBACK OK")

comp4.release()


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — ATOMIC RELEASE (context manager)
# ─────────────────────────────────────────────────────────────────────────────

section("TEST 5 — ATOMIC RELEASE: all devices return to available on context exit")

composer5 = make_composer()
plan5 = composer5.plan("vision")

print("\n  Entering `with composer5.bind(plan5) as comp5:` …")
with composer5.bind(plan5) as comp5:
    bound_nodes_5 = [label(h.node_id) for h in comp5.bound_blueprint.hops]
    bound_caps_5  = [(h.node_id, h.capability_name) for h in comp5.bound_blueprint.hops]
    print(f"  Inside context: bound={bound_nodes_5}")
    r5 = composer5.run(comp5)
    assert r5["ok"]
    print(f"  run() ok={r5['ok']}  consumer_confirmed={r5['consumer_confirmed']}")
    # broker should show slots occupied here
    for nid, cap in bound_caps_5:
        rt_check = node_to_rt[nid]
        occupied = len(rt_check.broker.active_binds.get(cap, [])) > 0
        print(f"  [inside]  {label(nid):12s}/{cap}: occupied={occupied}")

print("  Exited context — __exit__ released all parts")
for nid, cap in bound_caps_5:
    rt_check = node_to_rt[nid]
    free = broker_free(rt_check, cap)
    print(f"  [outside] {label(nid):12s}/{cap}: free={free}")
    assert free, f"LEAK: {label(nid)}/{cap} still occupied after context exit!"

assert comp5._released, "Composition._released should be True after context exit"
print("\n  ALL PARTS RELEASED")


print(f"\n{DIVIDER}")
print("  CAPABILITY COMPOSITION PART 2 OK")
print(DIVIDER)
