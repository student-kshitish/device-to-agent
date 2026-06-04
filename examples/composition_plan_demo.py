"""
Capability Composition — Plan Phase demo (Part 1 of 2).
Simulates a heterogeneous pool; no real devices, no binding.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d2a.contracts import IOContract
from d2a.composer import Composer, CompositionPlan
from d2a.composition.scorer import CapabilityScorer
from d2a.composition.cost_evaluator import CostEvaluator, Blueprint
from d2a.composition.contract_checker import ContractChecker
from d2a.composition.adapter_generator import AdapterGenerator


# ── Simulated capability pool ─────────────────────────────────────────────────

def make_pool():
    return [
        # drone-cam: raw_rgb 1280x720 @30fps, healthy (low load/temp)
        {
            "node_id":      "drone-cam",
            "capability":   "camera",
            "role":         "producer",
            "contract":     IOContract(media="image", format="raw_rgb",
                                       shape=(1280, 720, 3), rate=30.0),
            "device_class": "embedded",
            "live_state": {
                "sample_temps_c": [38.0],
                "load1": 0.3,
                "cpu_count": 4,
                "mem_used_percent": 30.0,
            },
        },
        # pi-cam: jpeg 1920x1080 @15fps, warmer + higher load
        {
            "node_id":      "pi-cam",
            "capability":   "camera",
            "role":         "producer",
            "contract":     IOContract(media="image", format="jpeg",
                                       shape=(1920, 1080, 3), rate=15.0),
            "device_class": "embedded",
            "live_state": {
                "sample_temps_c": [72.0],
                "load1": 1.8,
                "cpu_count": 4,
                "mem_used_percent": 65.0,
            },
        },
        # gpu-box-1: model consumer, accepts float32 tensor 640x480x3, healthy
        {
            "node_id":      "gpu-box-1",
            "capability":   "gpu",
            "role":         "consumer",
            "contract":     IOContract(media="tensor", format="float32",
                                       shape=(640, 480, 3), rate=None),
            "device_class": "server",
            "live_state": {
                "sample_temps_c": [45.0],
                "load1": 0.2,
                "cpu_count": 16,
                "mem_used_percent": 20.0,
            },
        },
        # gpu-box-2: same model consumer, but hot + high load
        {
            "node_id":      "gpu-box-2",
            "capability":   "gpu",
            "role":         "consumer",
            "contract":     IOContract(media="tensor", format="float32",
                                       shape=(640, 480, 3), rate=None),
            "device_class": "server",
            "live_state": {
                "sample_temps_c": [88.0],
                "load1": 14.0,
                "cpu_count": 16,
                "mem_used_percent": 85.0,
            },
        },
        # mic-node: audio pcm16 producer
        {
            "node_id":      "mic-node",
            "capability":   "microphone",
            "role":         "producer",
            "contract":     IOContract(media="audio", format="pcm16",
                                       shape=None, rate=None),
            "device_class": "embedded",
            "live_state": {
                "sample_temps_c": [35.0],
                "load1": 0.1,
                "cpu_count": 2,
                "mem_used_percent": 15.0,
            },
        },
    ]


DIVIDER = "=" * 68


def section(title: str):
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


# ── TEST 1: plan("vision") — full pipeline, scorer preference shown ───────────

section("TEST 1 — plan('vision'): full primary blueprint")

scorer = CapabilityScorer()
pool = make_pool()

# Show scorer reasoning explicitly
print("\n[Scorer] Candidate scores for each role:")
role_specs_vision = [
    {"role": "producer", "media": "image", "label": "camera"},
    {"role": "consumer", "label": "model",
     "contract": IOContract(media="tensor", format="float32", shape=(640, 480, 3))},
]
for spec in role_specs_vision:
    print(f"\n  role={spec['role']} media={spec.get('media','any')}")
    for entry in pool:
        if entry["role"] == spec["role"]:
            s = scorer.score(entry, spec)
            print(f"    {entry['node_id']:12s}  score={s:.4f}  "
                  f"temps={entry['live_state'].get('sample_temps_c')}  "
                  f"load={entry['live_state'].get('load1')}  "
                  f"mem={entry['live_state'].get('mem_used_percent')}%")

composer = Composer(make_pool)
plan = composer.plan("vision")

print()
print(plan.describe())


# ── TEST 2: both cameras produce valid blueprints with different chains ───────

section("TEST 2 — Both cameras: different adapter chains, cost comparison")

checker = ContractChecker()
adapter_gen = AdapterGenerator()
cost_eval = CostEvaluator()

# Build ranked candidates manually for full visibility
from d2a.composition.discovery import Discovery
from d2a.composition.goal_planner import GoalPlanner

gp = GoalPlanner()
disc = Discovery()
role_specs = gp.plan_requirements("vision")
all_candidates = disc.find_providers(role_specs, pool)
ranked = {i: scorer.rank(cands, role_specs[i]) for i, cands in all_candidates.items()}

blueprints = cost_eval.enumerate_blueprints(ranked, checker, adapter_gen, goal="vision")
print(f"\nAll enumerated blueprints ({len(blueprints)} total):\n")
for i, bp in enumerate(blueprints):
    print(f"Blueprint #{i+1}  valid={bp.valid}  cost={bp.total_cost:.3f}")
    for hop in bp.hops:
        chain_str = " → ".join(a.describe() for a in hop.adapter_chain) or "exact"
        print(f"  hop[{hop.role_index}] {hop.node_id:12s}  score={hop.score:.4f}  "
              f"chain: {chain_str}")
    if not bp.valid:
        print(f"  REJECTED: {bp.reject_reason}")
    print()

best = cost_eval.best(blueprints)
print(f"[CostEvaluator] BEST blueprint: "
      f"nodes={[h.node_id for h in best.hops]}  cost={best.total_cost:.3f}")


# ── TEST 3: fallbacks with different providers ─────────────────────────────────

section("TEST 3 — Fallback blueprints using different providers")

from d2a.composition.fallback_planner import FallbackPlanner
fp = FallbackPlanner()
primary, fallbacks = fp.plan(blueprints)

print(f"\nPrimary blueprint (cost={primary.total_cost:.3f}):")
print(f"  nodes: {[h.node_id for h in primary.hops]}")
for hop in primary.hops:
    chain_str = " → ".join(a.describe() for a in hop.adapter_chain) or "exact"
    print(f"  hop[{hop.role_index}] {hop.node_id:12s}  chain: {chain_str}")

for i, fb in enumerate(fallbacks):
    print(f"\nFallback {i+1} (cost={fb.total_cost:.3f}):")
    print(f"  nodes: {[h.node_id for h in fb.hops]}")
    overlap = set(fb.provider_ids()) & set(primary.provider_ids())
    print(f"  shared providers with primary: {overlap or 'none'}")
    for hop in fb.hops:
        chain_str = " → ".join(a.describe() for a in hop.adapter_chain) or "exact"
        print(f"  hop[{hop.role_index}] {hop.node_id:12s}  chain: {chain_str}")


# ── TEST 4: MISMATCH — remove cameras, only mic left ─────────────────────────

section("TEST 4 — MISMATCH: vision goal with only mic-node in pool")

mic_only_pool = [e for e in pool if e["node_id"] == "mic-node"]
# also keep gpu-box-1 to show the failure is on the producer side
mic_only_pool += [e for e in pool if e["node_id"] == "gpu-box-1"]

composer_mismatch = Composer(lambda: mic_only_pool)
plan_mismatch = composer_mismatch.plan("vision")

print()
if not plan_mismatch.ok:
    print(f"PLAN REJECTED: {plan_mismatch.reason}")
else:
    print("ERROR: expected rejection but got a plan!")
    sys.exit(1)

print(f"\n  stages_log:")
for s in plan_mismatch.stages_log:
    print(f"    {s}")


print(f"\n{DIVIDER}")
print("  COMPOSITION PLAN PART 1 OK")
print(DIVIDER)
