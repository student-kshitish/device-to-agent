"""
tests/test_derive_metrics.py — Phase 6 of CASE 4: observed-cost ranking + quarantine.

The deferred cost optimizer becomes real from data the system already generates —
health snapshots, heal counts, staleness, conformance verdicts. This suite proves:

  * MetricsStore is a bounded, persistent, per-recipe record (one disk write per
    record_* call — NO per-frame writes; survives a process restart).
  * The planner ranks WITHIN a preference tier by observed cost:
        tier (real > derived, structural)  >  observed score  >  cost_rank_hint  >  inputs
    A no-history recipe ranks by hint alone (cold start honest); a real provider is
    chosen before any recipe/metrics logic runs (fidelity honesty outranks measured
    reliability — the strict invariant).
  * Quarantine engages at the documented threshold (or a failed conformance run),
    is NEVER silent (surfaced in the refusal / registry), blocks planning without an
    explicit opt-in, and clears on a passing conformance run.
  * explain() names the single deciding factor between the pick and the runner-up.
  * A live derivation folds its run into the store exactly once (bounded write), and
    a FAILED transition records the run durably even without a clean close().

Hermetic: module redirects D2A_HOME to a throwaway dir; every store/registry/trust
path is explicit tmp, so nothing touches the real ~/.d2a.
"""

import json
import os
import shutil
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from d2a import crypto, manifest as d2a_manifest
from d2a_derive import (
    Registry, Planner, TrustStore, MetricsStore, sign_recipe, errors,
    run_conformance, explain, format_explanation,
)
from d2a_derive.metrics import QUARANTINE_MIN_RUNS
from d2a_derive.planner import ranking_key

# reuse the live harness + the nondeterministic conformance fixture verbatim.
from tests.test_derive_live import LiveLAN
from tests.test_derive_distribution import (
    _NONDET_RECIPE, _NONDET_TRANSFORM, _NONDET_FRAMES,
)
from tests._env import use_tmp_home, restore_home

_REF = Path(__file__).resolve().parent.parent / "d2a_derive" / "reference_recipes"


def setUpModule():
    use_tmp_home()


def tearDownModule():
    restore_home()


class _Cap:
    def __init__(self, name):
        self.name = name
        self.live_state = {}


def _sensing_manifest():
    return d2a_manifest.builtin_manifest(_Cap("sensing"))


def _provider(node_id, name, manifest):
    return {"node_id": node_id, "name": name, "manifest": manifest}


# ── MetricsStore unit: bounded writes, persistence, quarantine policy ──────────

class TestMetricsStore(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "m.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_history_scores_zero(self):
        s = MetricsStore(path=self.path)
        self.assertEqual(s.observed_score("unseen"), (0.0, 0.0, 0.0))
        self.assertFalse(s.is_quarantined("unseen"))

    def test_each_record_is_exactly_one_write(self):
        s = MetricsStore(path=self.path)
        self.assertEqual(s.writes, 0)
        s.record_run("r", uptime=1.0, heal_count=0, failed=False, staleness=0.1)
        self.assertEqual(s.writes, 1)
        s.record_run("r", uptime=1.0, heal_count=2, failed=False, staleness=0.2)
        self.assertEqual(s.writes, 2)
        s.record_conformance("r", True)
        self.assertEqual(s.writes, 3)

    def test_metrics_survive_restart(self):
        s = MetricsStore(path=self.path)
        s.record_run("r", uptime=5.0, heal_count=3, failed=True, staleness=0.4)
        # a FRESH store over the same file reloads the record.
        s2 = MetricsStore(path=self.path)
        rec = s2.get("r")
        self.assertEqual(rec.runs, 1)
        self.assertEqual(rec.heal_count, 3)
        self.assertEqual(rec.failed_count, 1)
        self.assertAlmostEqual(rec.total_uptime, 5.0)

    def test_quarantine_needs_meaningful_run_count(self):
        s = MetricsStore(path=self.path)
        # all-failing but below the minimum run count → NOT quarantined (cold-start safe)
        for _ in range(QUARANTINE_MIN_RUNS - 1):
            s.record_run("r", uptime=1.0, heal_count=0, failed=True, staleness=0.0)
        self.assertFalse(s.is_quarantined("r"))
        # the run that reaches the threshold with failure_rate > 0.5 engages it.
        s.record_run("r", uptime=1.0, heal_count=0, failed=True, staleness=0.0)
        self.assertTrue(s.is_quarantined("r"))

    def test_low_failure_rate_never_quarantines(self):
        s = MetricsStore(path=self.path)
        s.record_run("r", uptime=1.0, heal_count=0, failed=True, staleness=0.0)
        for _ in range(4):
            s.record_run("r", uptime=1.0, heal_count=0, failed=False, staleness=0.0)
        self.assertFalse(s.is_quarantined("r"))   # 1/5 = 0.2 ≤ 0.5

    def test_conformance_fail_quarantines_pass_clears(self):
        s = MetricsStore(path=self.path)
        s.record_conformance("r", False)
        self.assertTrue(s.is_quarantined("r"))
        self.assertFalse(s.get("r").last_conformance["passed"])
        s.record_conformance("r", True)
        self.assertFalse(s.is_quarantined("r"))
        self.assertTrue(s.get("r").last_conformance["passed"])


# ── ranking key: tier > observed > hint > inputs (structural order) ────────────

class TestRankingKey(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.s = MetricsStore(path=Path(self._tmp.name) / "m.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _lr(self, name, hint, n_inputs):
        return SimpleNamespace(recipe_name=name, cost_rank_hint=hint,
                               requires=[{}] * n_inputs)

    def test_observed_dominates_hint(self):
        # a flaky recipe with a BETTER (lower) hint still loses to a clean one.
        flaky = self._lr("flaky", hint=1, n_inputs=1)
        clean = self._lr("clean", hint=9, n_inputs=1)
        self.s.record_run("flaky", uptime=1.0, heal_count=0, failed=True, staleness=0.0)
        self.assertLess(ranking_key(self.s, clean), ranking_key(self.s, flaky))

    def test_hint_dominates_inputs_when_observed_ties(self):
        # both cold (observed 0,0,0): the lower hint wins even with MORE inputs.
        cheap = self._lr("cheap", hint=1, n_inputs=3)
        pricey = self._lr("pricey", hint=9, n_inputs=1)
        self.assertLess(ranking_key(self.s, cheap), ranking_key(self.s, pricey))

    def test_inputs_break_the_final_tie(self):
        few = self._lr("few", hint=5, n_inputs=1)
        many = self._lr("many", hint=5, n_inputs=2)
        self.assertLess(ranking_key(self.s, few), ranking_key(self.s, many))


# ── planner: observed ranking + cold start + the real-beats-derived invariant ──

class PlannerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.recipes_dir = Path(self._tmp.name) / "recipes"
        self.recipes_dir.mkdir(parents=True)
        self.priv, self.pub = crypto.generate_keypair()
        self.trust = TrustStore(path=Path(self._tmp.name) / "trust.json")
        self.store = MetricsStore(path=Path(self._tmp.name) / "m.json")

    def tearDown(self):
        self._tmp.cleanup()

    def install_thermal(self, name, cost_rank_hint=None):
        """Copy the shipped thermal recipe under a fresh package name (so two admitted
        recipes provide the SAME capability 'ambient_temp') and re-sign it."""
        dst = self.recipes_dir / name
        shutil.copytree(_REF / "thermal_ambient_proxy", dst)
        recipe = json.loads((dst / "recipe.json").read_text())
        recipe["name"] = name
        recipe["provides"]["recipe"] = name
        if cost_rank_hint is not None:
            recipe["cost_rank_hint"] = cost_rank_hint
        (dst / "recipe.json").write_text(json.dumps(sign_recipe(recipe, self.priv)))
        self.trust.add(self.pub, "test")
        return name

    def registry(self):
        return Registry(recipes_dir=self.recipes_dir, trust=self.trust)

    def planner(self, *, include_quarantined=False):
        prov = _provider("nodeS", "sensing", _sensing_manifest())
        return Planner(self.registry(),
                       discover=lambda n: {"sensing": [prov]}.get(n, []),
                       metrics=self.store, include_quarantined=include_quarantined)


class TestObservedRanking(PlannerBase):
    def test_cold_start_ranks_by_hint(self):
        self.install_thermal("cheap", cost_rank_hint=5)
        self.install_thermal("pricey", cost_rank_hint=20)
        res = self.planner().need("ambient_temp")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.recipe.recipe_name, "cheap")   # lower hint, no history

    def test_observed_history_overrides_hint(self):
        self.install_thermal("cheap", cost_rank_hint=5)
        self.install_thermal("pricey", cost_rank_hint=20)
        # the cheaper-by-hint recipe has FAILED here; the pricier one is clean → the
        # clean one is chosen DESPITE the worse hint (observed cost outranks the hint).
        self.store.record_run("cheap", uptime=1.0, heal_count=0, failed=True, staleness=0.0)
        res = self.planner().need("ambient_temp")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.recipe.recipe_name, "pricey")

    def test_real_provider_beats_even_a_flawless_derived(self):
        # the strict invariant: a real provider is chosen in step 1, before any
        # recipe/metrics logic — a perfect observed record can never lift a derivation
        # over a real provider.
        self.install_thermal("thermal_ambient_proxy")
        self.store.record_run("thermal_ambient_proxy", uptime=99.0, heal_count=0,
                              failed=False, staleness=0.0)      # flawless derived history
        direct = _provider("nodeD", "ambient_temp", _sensing_manifest())
        pl = Planner(self.registry(),
                     discover=lambda n: {"ambient_temp": [direct],
                                         "sensing": [direct]}.get(n, []),
                     metrics=self.store)
        res = pl.need("ambient_temp")
        self.assertEqual(res.outcome, "direct")
        self.assertIsNone(res.plan)


class TestQuarantinePlanner(PlannerBase):
    def test_quarantined_recipe_blocks_planning_and_names_it(self):
        self.install_thermal("thermal_ambient_proxy")
        self.store.record_conformance("thermal_ambient_proxy", False)   # quarantine
        res = self.planner().need("ambient_temp")
        self.assertEqual(res.outcome, "refused")
        self.assertEqual(res.code, errors.RECIPE_QUARANTINED)
        self.assertIn("thermal_ambient_proxy", res.detail)              # never silent

    def test_include_quarantined_opt_in_plans_it(self):
        self.install_thermal("thermal_ambient_proxy")
        self.store.record_conformance("thermal_ambient_proxy", False)
        res = self.planner(include_quarantined=True).need("ambient_temp")
        self.assertEqual(res.outcome, "derived")

    def test_passing_conformance_clears_quarantine(self):
        self.install_thermal("thermal_ambient_proxy")
        self.store.record_conformance("thermal_ambient_proxy", False)
        self.assertEqual(self.planner().need("ambient_temp").outcome, "refused")
        # a passing conformance run is the documented all-clear.
        self.store.record_conformance("thermal_ambient_proxy", True)
        self.assertEqual(self.planner().need("ambient_temp").outcome, "derived")

    def test_one_quarantined_does_not_hide_a_good_sibling(self):
        self.install_thermal("bad", cost_rank_hint=5)
        self.install_thermal("good", cost_rank_hint=20)
        self.store.record_conformance("bad", False)      # quarantine the cheaper one
        res = self.planner().need("ambient_temp")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.recipe.recipe_name, "good")   # falls through to sibling


# ── explain(): names the deciding factor ──────────────────────────────────────

class TestExplain(PlannerBase):
    def _explain(self, **kw):
        return explain("ambient_temp", recipes_dir=self.recipes_dir,
                       trust=self.trust, metrics=self.store, **kw)

    def test_explain_cold_start_by_hint(self):
        self.install_thermal("cheap", cost_rank_hint=5)
        self.install_thermal("pricey", cost_rank_hint=20)
        exp = self._explain()
        self.assertTrue(exp["cold_start"])
        self.assertEqual(exp["pick"], "cheap")
        self.assertIn("cost_rank_hint", exp["deciding_factor"])
        # it renders without error and mentions the pick.
        self.assertIn("cheap", format_explanation(exp))

    def test_explain_names_failure_rate_as_the_decider(self):
        self.install_thermal("cheap", cost_rank_hint=5)
        self.install_thermal("pricey", cost_rank_hint=20)
        self.store.record_run("cheap", uptime=1.0, heal_count=0, failed=True, staleness=0.0)
        exp = self._explain()
        self.assertFalse(exp["cold_start"])
        self.assertEqual(exp["pick"], "pricey")
        self.assertEqual(exp["runner_up"], "cheap")
        self.assertIn("failure rate", exp["deciding_factor"])

    def test_explain_excludes_quarantined(self):
        self.install_thermal("cheap", cost_rank_hint=5)
        self.install_thermal("pricey", cost_rank_hint=20)
        self.store.record_conformance("cheap", False)
        exp = self._explain()
        self.assertEqual(exp["excluded_quarantined"], ["cheap"])
        self.assertEqual(exp["pick"], "pricey")

    def test_explain_reports_direct_provider_would_win(self):
        self.install_thermal("thermal_ambient_proxy")
        direct = _provider("nodeD", "ambient_temp", _sensing_manifest())
        exp = self._explain(discover=lambda n: {"ambient_temp": [direct]}.get(n, []))
        self.assertTrue(exp["direct_provider"])


# ── conformance runner folds the verdict into the metrics store ────────────────

class TestConformanceMetrics(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.recipes_dir = Path(self._tmp.name) / "recipes"
        self.recipes_dir.mkdir(parents=True)
        self.priv, self.pub = crypto.generate_keypair()
        self.trust = TrustStore(path=Path(self._tmp.name) / "trust.json")
        self.store = MetricsStore(path=Path(self._tmp.name) / "m.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _install_thermal(self):
        dst = self.recipes_dir / "thermal_ambient_proxy"
        shutil.copytree(_REF / "thermal_ambient_proxy", dst)
        recipe = json.loads((dst / "recipe.json").read_text())
        (dst / "recipe.json").write_text(json.dumps(sign_recipe(recipe, self.priv)))
        self.trust.add(self.pub, "test")

    def _install_flaky(self):
        dst = self.recipes_dir / "flaky"
        dst.mkdir(parents=True)
        recipe = json.loads(json.dumps(_NONDET_RECIPE))   # deep copy (don't mutate the shared fixture)
        (dst / "recipe.json").write_text(json.dumps(sign_recipe(recipe, self.priv)))
        (dst / "transform.py").write_text(_NONDET_TRANSFORM)
        (dst / "test_frames.json").write_text(json.dumps(_NONDET_FRAMES))
        self.trust.add(self.pub, "test")

    def test_passing_conformance_records_and_clears(self):
        self._install_thermal()
        self.store.record_conformance("thermal_ambient_proxy", False)   # pre-quarantined
        report = run_conformance("thermal_ambient_proxy", recipes_dir=self.recipes_dir,
                                 trust=self.trust, live=False, metrics=self.store)
        self.assertTrue(report["passed"])
        self.assertFalse(self.store.is_quarantined("thermal_ambient_proxy"))
        self.assertTrue(self.store.get("thermal_ambient_proxy").last_conformance["passed"])

    def test_failing_conformance_quarantines(self):
        self._install_flaky()
        report = run_conformance("flaky", recipes_dir=self.recipes_dir,
                                 trust=self.trust, live=False, metrics=self.store)
        self.assertFalse(report["passed"])
        self.assertTrue(self.store.is_quarantined("flaky"))

    def test_record_metrics_false_is_side_effect_free(self):
        self._install_thermal()
        run_conformance("thermal_ambient_proxy", recipes_dir=self.recipes_dir,
                        trust=self.trust, live=False, metrics=self.store,
                        record_metrics=False)
        self.assertEqual(self.store.get("thermal_ambient_proxy").last_conformance, None)
        self.assertEqual(self.store.writes, 0)


# ── live: bounded write at close, durable record at a FAILED transition ────────

class TestLiveMetrics(LiveLAN, unittest.TestCase):
    def _store(self):
        return MetricsStore(path=Path(self._home.name) / "metrics.json")

    def test_live_run_writes_metrics_exactly_once(self):
        self.install_reference("thermal_ambient_proxy")
        dev = self.make_device("dev", caps=("compute", "sensing"))
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev)
        res = pl.need("ambient_temp")
        self.assertEqual(res.outcome, "derived")

        store = self._store()
        dc = self.run_derived(res.plan, ag, metrics=store, monitor_interval_s=0.05)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))
        # let many frames flow — a per-frame write would blow the counter up.
        time.sleep(0.8)
        self.assertEqual(store.writes, 0, "metrics written mid-run (should be in-memory)")
        # lifetime summary is surfaced in health() while running.
        self.assertIn("lifetime", dc.health())

        dc.close()
        self.assertEqual(store.writes, 1, "close() must persist the run exactly once")
        rec = store.get("thermal_ambient_proxy")
        self.assertEqual(rec.runs, 1)
        self.assertEqual(rec.failed_count, 0)
        self.assertGreater(rec.total_uptime, 0.0)

    def test_failed_transition_records_run_once(self):
        # dual fixture: required sensing + optional odometry on SEPARATE devices, so
        # we can kill the required input and drive the capability to FAILED.
        self.install_dual()
        dev_s = self.make_device("dev-sensing", caps=("compute", "sensing"))
        dev_o = self.make_device("dev-odo", caps=("compute",), odo_unit="m")
        ag = self.make_agent("ag")
        pl = self.planner(ag, dev_s, dev_o)
        res = pl.need("dual_metric")
        self.assertEqual(res.outcome, "derived")

        store = self._store()
        dc = self.run_derived(res.plan, ag, metrics=store,
                              heal_max_attempts=2, heal_shutdown_backoff_s=1.0)
        self.assertTrue(self.wait_until(lambda: dc.reading() is not None))

        dev_s.stop_swarm()                               # kill the REQUIRED input
        self.assertTrue(self.wait_until(lambda: dc.state == "failed", timeout=8.0),
                        "required input gone did not fail the capability")
        # the FAILED transition persists the run durably, even before close().
        self.assertTrue(self.wait_until(
            lambda: store.get("dual_probe_fixture").failed_count == 1, timeout=5.0),
            "failed run was not recorded at the transition")
        self.assertEqual(store.writes, 1)

        dc.close()                                       # once-guard: no double count
        self.assertEqual(store.writes, 1)
        self.assertEqual(store.get("dual_probe_fixture").runs, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
