"""
d2a_derive/conformance.py — the recipe conformance runner.

    python -m d2a_derive.conformance <name>

A signature proves authorship; the dry-run gate proves a recipe passes its OWN
frames; conformance proves a bit more, and produces the ARTIFACT a future community
PR review would attach: a machine-readable report that the recipe behaves both in
the deterministic dry-run AND, when its inputs can be stood up locally, in a bounded
LIVE run against a real DeviceRuntime + RemoteAgent.

Two checks, honest about what each proves:

  1. DRY-RUN (deterministic, twice). The recipe's dry-run gate (dryrun.py already
     runs the frames twice to catch hidden state) is itself run TWICE — a second,
     independent invocation must yield an identical sample output. This catches
     nondeterminism that survives a single gate (e.g. dict ordering that happens to
     match within one process but not across fresh module state).

  2. LIVE (bounded, N seconds) — ONLY when the recipe's `requires` are satisfiable
     by live LOCAL capabilities. We stand up one DeviceRuntime offering the real
     shipped capabilities (`compute`, `sensing`) plus the demo odometry scaffolding,
     plan the derivation, and run it for N seconds asserting: the transform emits,
     every output validates against `provides`, no exception escapes, the capability
     stays `active`, and no input exceeds its staleness bound. A recipe whose inputs
     cannot be produced locally is not a failure — live is reported `ran: false` with
     a reason.

REPORT shape (printed as JSON): {recipe, version, dry_run, live, environment}. Exit
0 iff the recipe PASSES (dry-run ok, and live either passed or was legitimately not
run); exit 1 otherwise. This is what makes a broken recipe fail CI.
"""

import argparse
import json
import platform
import sys
import time
from pathlib import Path

from d2a import crypto
from d2a_derive import errors
from d2a_derive.dryrun import _validate_output
from d2a_derive.metrics import MetricsStore
from d2a_derive.registry import Registry
from d2a_derive.trust import TrustStore

# Bounded live-run budget (seconds). A conformance run must terminate quickly; this
# is long enough for the incremental transforms to emit several frames.
DEFAULT_LIVE_SECONDS = 2.0

# Local capabilities we can stand up to satisfy a recipe's requires. `compute` and
# `sensing` are the real shipped capabilities every Linux host offers; demo_odometry
# is the honest synthetic scaffolding for the one capability the world does not ship.
_LOCAL_REAL_CAPS = ("compute", "sensing")


def run_conformance(name: str, *, recipes_dir=None, trust: TrustStore | None = None,
                    live_seconds: float = DEFAULT_LIVE_SECONDS,
                    live: bool = True, metrics: MetricsStore | None = None,
                    record_metrics: bool = True) -> dict:
    """
    Run the conformance checks for installed recipe `name` and return the report
    dict {recipe, version, dry_run, live, environment, passed}. Never raises for a
    recipe fault — a failure is reported in the dict (dry_run.ok / live.ok false).

    Phase 6: unless `record_metrics` is False, the verdict is folded into the recipe's
    observed-runtime record (`metrics`, default the shared store): a PASS is the
    authoritative all-clear that CLEARS any quarantine (the documented way to reinstate
    a recipe); a FAIL quarantines it directly. Only recorded once the recipe actually
    loads (a package that never admits can't be planned anyway).
    """
    recipes_dir = Path(recipes_dir) if recipes_dir is not None \
        else (crypto.d2a_home() / "recipes")
    trust = trust if trust is not None else TrustStore()

    report = {
        "recipe": name,
        "version": "",
        "dry_run": {"ok": False, "reason": "not run"},
        "live": {"ran": False, "reason": "not attempted"},
        "environment": _environment(),
    }

    # Load + admit the recipe through the SAME registry pipeline a real load uses, so
    # conformance never diverges from what the engine will actually accept.
    reg = Registry(recipes_dir=recipes_dir, trust=trust, auto_load=False)
    pkg_dir = recipes_dir / name
    if not pkg_dir.is_dir():
        report["dry_run"] = {"ok": False, "reason": f"recipe '{name}' is not installed "
                                                    f"in {recipes_dir}"}
        report["passed"] = False
        return report
    try:
        lr = reg.load_one(pkg_dir)
    except errors.DeriveError as exc:
        report["dry_run"] = {"ok": False, "code": exc.code, "reason": exc.detail}
        report["passed"] = False
        return report

    report["version"] = lr.version
    reg._index(lr)                       # make it discoverable for the live planner

    # 1. DRY-RUN, deterministic, twice.
    report["dry_run"] = _dry_run_twice(lr)

    # 2. LIVE (best-effort, only if inputs are locally satisfiable). The planner is
    #    keyed by the capability a recipe PROVIDES (provided_name), which is not
    #    necessarily the package/dir name — plan for what this recipe provides.
    if live:
        report["live"] = _live_run(lr.provided_name, reg, trust, live_seconds)

    dry_ok = report["dry_run"]["ok"]
    live_ok = (not report["live"]["ran"]) or report["live"].get("ok", False)
    report["passed"] = bool(dry_ok and live_ok)

    # Phase 6: record the verdict against the recipe's OWN name (recipe_name, the
    # metrics key), so a pass clears / a fail engages the quarantine flag the planner
    # and `registry list/show` read. Advisory — never let a metrics fault mask the
    # report the caller asked for.
    if record_metrics:
        store = metrics if metrics is not None else MetricsStore()
        try:
            store.record_conformance(lr.recipe_name, report["passed"])
        except Exception:                            # noqa: BLE001
            pass
    return report


# ── check 1: dry-run twice ────────────────────────────────────────────────────

def _dry_run_twice(lr) -> dict:
    """Run the dry-run gate twice from fresh module state and require an identical
    sample output. lr.dry_run is the admission run (run #1); we do run #2 here."""
    from d2a_derive.dryrun import dry_run
    from d2a_derive.loader import load_transform

    first = lr.dry_run
    if not first.ok:
        return {"ok": False, "runs": 1, "reason": first.reason}

    # Reload the transform into a fresh module and re-run — determinism must hold
    # across independent module state, not merely within one loaded module.
    try:
        module2 = load_transform(lr.pkg)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "runs": 1, "reason": f"reload for run 2 failed: {exc}"}
    second = dry_run(lr.pkg, module2, lr.manifest)
    if not second.ok:
        return {"ok": False, "runs": 2, "reason": f"run 2 failed: {second.reason}"}
    if first.sample_output != second.sample_output:
        return {"ok": False, "runs": 2,
                "reason": f"nondeterministic across reloads: {first.sample_output} "
                          f"!= {second.sample_output}"}
    return {"ok": True, "runs": 2, "sample_output": second.sample_output}


# ── check 2: bounded live run ─────────────────────────────────────────────────

def _live_run(provided_name: str, reg: Registry, trust: TrustStore, seconds: float) -> dict:
    """Stand up local capabilities, plan the derivation of `provided_name` (the
    capability the recipe PROVIDES — the planner's lookup key, not the package dir
    name), and run it for `seconds`, asserting output validity, no exceptions, active
    state, and bounded staleness. Returns {ran, ok, reason, ...}. A recipe whose
    inputs can't be produced locally reports ran=False (not a failure)."""
    # Imported lazily: these pull in the transport + device runtime, which the
    # derivation engine core deliberately does not import at module load.
    from d2a_derive.planner import Planner
    from d2a_derive.executor import DerivedCapability, ACTIVE
    from d2a_derive.demo_scaffolding import register_demo_odometry, OdometrySource
    from runtimes.device_runtime import DeviceRuntime
    from agents.remote_agent import RemoteAgent

    device = agent = dc = None
    try:
        device = DeviceRuntime(name="conformance-dev",
                               capability_override=list(_LOCAL_REAL_CAPS), lease_ttl=60)
        register_demo_odometry(device, unit="m",
                               source=OdometrySource(unit="m", step_m=0.6, turn=0.7))
        device.start_swarm()

        agent = RemoteAgent(name="conformance-agent", auto_renew=True)
        agent.start()

        # seed discovery LAN-style (the same shape tests use): copy the device's
        # signed records into the agent's swarm and register it as a known peer.
        ip, port = device.swarm.address
        with agent.swarm._lock:
            for c in device.advertise():
                agent.swarm.records[(device.node_id, c.name)] = \
                    device._capability_record(c, ip, port)
        agent.swarm.add_known_peer(device.node_id, ip, port)

        def discover(cap_name):
            with agent.swarm._lock:
                return [dict(r) for (nid, nm), r in agent.swarm.records.items()
                        if nm == cap_name and isinstance(r.get("manifest"), dict)]

        planner = Planner(reg, discover=discover)
        res = planner.need(provided_name)
        if res.outcome != "derived":
            return {"ran": False,
                    "reason": f"inputs not satisfiable by local capabilities "
                              f"(need() → {res.outcome}"
                              f"{'/' + res.code if res.code else ''})"}

        dc = DerivedCapability(res.plan, agent, staleness_factor=3.0,
                               monitor_interval_s=0.1)
        dc.start()

        # wait (bounded) for the first emission, then let it run out the budget.
        deadline = time.time() + seconds
        while time.time() < deadline and dc.reading() is None:
            time.sleep(0.05)
        first_emit = dc.reading() is not None
        # let a few more frames flow so staleness/state settle.
        remaining = max(0.0, deadline - time.time())
        if remaining:
            time.sleep(remaining)

        out = dc.reading()
        health = dc.health()
        state = dc.state

        outputs_valid, valid_reason = (False, "transform never emitted")
        if out is not None:
            outputs_valid, valid_reason = _validate_output(out, res.plan.manifest)

        # staleness bound: no input feed older than staleness_factor × its expected
        # interval (the same threshold the monitor degrades on).
        max_staleness = _max_input_staleness(health)

        ok = bool(first_emit and outputs_valid and state == ACTIVE)
        reason = "ok"
        if not first_emit:
            reason = "transform did not emit within the live budget"
        elif not outputs_valid:
            reason = f"live output does not validate against provides: {valid_reason}"
        elif state != ACTIVE:
            reason = f"capability not active (state={state})"

        return {
            "ran": True,
            "ok": ok,
            "reason": reason,
            "effective_tier": res.plan.effective_tier,
            "depth": res.plan.depth,
            "sample_output": out,
            "state": state,
            "outputs_valid": outputs_valid,
            "max_input_staleness_s": max_staleness,
            "seconds": seconds,
        }
    except Exception as exc:                          # noqa: BLE001 — any live fault = fail
        return {"ran": True, "ok": False, "reason": f"exception during live run: "
                                                    f"{type(exc).__name__}: {exc}"}
    finally:
        for obj, meth in ((dc, "close"), (agent, "stop"), (device, "stop_swarm")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:                     # noqa: BLE001
                    pass
        time.sleep(0.05)


def _max_input_staleness(health: dict):
    vals = [v.get("staleness_s") for v in health.get("per_input", {}).values()
            if v.get("staleness_s") is not None]
    return round(max(vals), 3) if vals else None


def _environment() -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "crypto_backend": crypto.ACTIVE_BACKEND,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m d2a_derive.conformance",
        description="Run the conformance checks for an installed recipe and emit a "
                    "machine-readable report.")
    ap.add_argument("name", help="the installed recipe name to check")
    ap.add_argument("--recipes-dir", default=None,
                    help="registry dir to load from (default <d2a_home>/recipes)")
    ap.add_argument("--seconds", type=float, default=DEFAULT_LIVE_SECONDS,
                    help=f"bounded live-run budget (default {DEFAULT_LIVE_SECONDS}s)")
    ap.add_argument("--no-live", action="store_true",
                    help="skip the live run (dry-run gate only)")
    args = ap.parse_args(argv)

    report = run_conformance(args.name, recipes_dir=args.recipes_dir,
                             live_seconds=args.seconds, live=not args.no_live)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
