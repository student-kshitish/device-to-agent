"""
tests/test_derive.py — Phase 1 of CASE 4 (capability derivation).

Covers the format + trust + validation + planning + dry-run + provenance layer.
NO hardware is bound in Phase 1: need() returns a PLAN. Live executor / self-heal
/ monitor / both-transports live in Phase 2.

Every test signs recipes into a TEMP recipes dir with a freshly-generated author
key (hermetic — robust to any future canonicalization change), and points a
TrustStore at a temp trusted_authors.json. Both reference recipes are copied from
the shipped packages so we exercise the real transforms.
"""

import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from d2a import crypto, manifest as d2a_manifest
from d2a_derive import (
    Registry, Planner, TrustStore,
    sign_recipe, verify_recipe_sig, effective_tier, errors,
    check_input_against_provider, DERIVE_MAX_INPUT_HZ,
)

_REPO = Path(__file__).resolve().parent.parent
_REF = _REPO / "d2a_derive" / "reference_recipes"


# ── provider manifests used as discovery fixtures ─────────────────────────────

def _sensing_manifest():
    """The REAL shipped 'sensing' manifest (open tier, has thermal.max_temp_c)."""
    return d2a_manifest.builtin_manifest(_Cap("sensing"))


class _Cap:
    def __init__(self, name):
        self.name = name
        self.live_state = {}


def _odometry_manifest(unit="m"):
    """A demo positional provider manifest (open tier). No shipped capability
    exposes position — this stands in for the Phase-2 demo_odometry source."""
    return {
        "description": "synthetic trajectory for demonstration",
        "reading": {
            "pose.x_m": {"type": "number", "unit": unit},
            "pose.y_m": {"type": "number", "unit": unit},
        },
        "consent_tier": "open",
        "streaming": True,
    }


def _provider(node_id, name, manifest):
    return {"node_id": node_id, "name": name, "manifest": manifest}


# ── base fixture: temp home, temp recipes dir, temp trust store ───────────────

class DeriveBase(unittest.TestCase):
    def setUp(self):
        self._home = TemporaryDirectory()
        os.environ["D2A_HOME"] = self._home.name
        self.recipes_dir = Path(self._home.name) / "recipes"
        self.recipes_dir.mkdir(parents=True, exist_ok=True)
        # a per-test author identity
        self.priv, self.pub = crypto.generate_keypair()
        self.trust = TrustStore(path=Path(self._home.name) / "trusted_authors.json")

    def tearDown(self):
        os.environ.pop("D2A_HOME", None)
        self._home.cleanup()

    # helpers ------------------------------------------------------------------

    def install_reference(self, name, *, sign=True, trust=True, author_priv=None):
        """Copy a shipped reference recipe into the temp recipes dir, (re)sign it
        with a test key, and optionally trust that author. Returns the dir."""
        dst = self.recipes_dir / name
        shutil.copytree(_REF / name, dst)
        priv = author_priv if author_priv is not None else self.priv
        if sign:
            recipe = json.loads((dst / "recipe.json").read_text())
            signed = sign_recipe(recipe, priv)
            (dst / "recipe.json").write_text(json.dumps(signed, indent=2))
            if trust:
                self.trust.add(crypto.public_from_private(priv), "test")
        return dst

    def registry(self):
        return Registry(recipes_dir=self.recipes_dir, trust=self.trust)


# ── signing round-trip ────────────────────────────────────────────────────────

class TestSigning(DeriveBase):
    def test_sign_verify_roundtrip(self):
        recipe = json.loads((_REF / "thermal_ambient_proxy" / "recipe.json").read_text())
        signed = sign_recipe(recipe, self.priv)
        self.assertTrue(verify_recipe_sig(signed))
        self.assertEqual(signed["author_pubkey"], self.pub)

    def test_tamper_breaks_signature(self):
        recipe = json.loads((_REF / "thermal_ambient_proxy" / "recipe.json").read_text())
        signed = sign_recipe(recipe, self.priv)
        signed["cost_rank_hint"] = signed["cost_rank_hint"] + 1   # tamper a signed field
        self.assertFalse(verify_recipe_sig(signed))

    def test_shipped_reference_recipes_are_selfconsistently_signed(self):
        # The committed DEMO-signed reference recipes verify against their own
        # embedded author_pubkey (authorship proof travels with the package).
        for name in ("thermal_ambient_proxy", "trajectory_free_space_map"):
            recipe = json.loads((_REF / name / "recipe.json").read_text())
            self.assertTrue(verify_recipe_sig(recipe), name)


# ── registry admission: trust + validation gates ──────────────────────────────

class TestRegistryAdmission(DeriveBase):
    def test_trusted_signed_recipe_loads(self):
        self.install_reference("thermal_ambient_proxy")
        reg = self.registry()
        self.assertEqual([r.provided_name for r in reg.loaded], ["ambient_temp"])
        self.assertEqual(reg.rejected, [])

    def test_unsigned_recipe_refused(self):
        dst = self.install_reference("thermal_ambient_proxy", sign=False, trust=False)
        # blank out the signature fields
        recipe = json.loads((dst / "recipe.json").read_text())
        recipe["sig"] = ""
        recipe["author_pubkey"] = ""
        (dst / "recipe.json").write_text(json.dumps(recipe))
        reg = self.registry()
        self.assertEqual(reg.loaded, [])
        self.assertEqual([r.code for r in reg.rejected], [errors.RECIPE_UNSIGNED])

    def test_bad_signature_refused(self):
        dst = self.install_reference("thermal_ambient_proxy")
        recipe = json.loads((dst / "recipe.json").read_text())
        # corrupt the signature (keep it hex + right length so it parses but fails)
        recipe["sig"] = "00" * 64
        (dst / "recipe.json").write_text(json.dumps(recipe))
        reg = self.registry()
        self.assertEqual([r.code for r in reg.rejected], [errors.RECIPE_BAD_SIG])

    def test_untrusted_author_refused(self):
        # signed correctly, but the author is NOT added to the trust store
        self.install_reference("thermal_ambient_proxy", trust=False)
        reg = self.registry()
        self.assertEqual([r.code for r in reg.rejected], [errors.RECIPE_UNTRUSTED_AUTHOR])

    def test_bad_sig_and_untrusted_are_distinct_codes(self):
        self.assertNotEqual(errors.RECIPE_BAD_SIG, errors.RECIPE_UNTRUSTED_AUTHOR)

    def test_bad_provides_vocabulary_refused_at_load(self):
        # A signed+trusted recipe whose provides carries an ILLEGAL manifest field
        # is refused at REGISTRY LOAD (RECIPE_INVALID), not at plan time.
        dst = self.install_reference("thermal_ambient_proxy", sign=False, trust=False)
        recipe = json.loads((dst / "recipe.json").read_text())
        recipe["provides"]["reading"]["ambient_trend_c"]["type"] = "not_a_type"
        signed = sign_recipe(recipe, self.priv)
        (dst / "recipe.json").write_text(json.dumps(signed))
        self.trust.add(self.pub, "test")
        reg = self.registry()
        self.assertEqual(reg.loaded, [])
        self.assertEqual([r.code for r in reg.rejected], [errors.RECIPE_INVALID])

    def test_one_bad_recipe_does_not_blind_the_good_one(self):
        self.install_reference("thermal_ambient_proxy")               # good (self.priv, trusted)
        # sign the second with a DIFFERENT author that is never trusted
        other_priv, _ = crypto.generate_keypair()
        self.install_reference("trajectory_free_space_map", trust=False, author_priv=other_priv)
        reg = self.registry()
        self.assertEqual([r.provided_name for r in reg.loaded], ["ambient_temp"])
        self.assertEqual(len(reg.rejected), 1)


# ── dry-run gate ──────────────────────────────────────────────────────────────

class TestDryRun(DeriveBase):
    def test_reference_recipes_pass_their_own_dryrun(self):
        for name in ("thermal_ambient_proxy", "trajectory_free_space_map"):
            self.install_reference(name)
        reg = self.registry()
        self.assertEqual(len(reg.loaded), 2)
        for lr in reg.loaded:
            self.assertTrue(lr.dry_run.ok, lr.recipe_name)

    def test_dryrun_failure_blocks_admission(self):
        # Break the transform so its output omits a declared field -> DRYRUN_FAILED,
        # and the recipe never enters the registry (so it can never bind hardware).
        dst = self.install_reference("thermal_ambient_proxy", sign=False, trust=False)
        (dst / "transform.py").write_text(
            "def init(ctx):\n    pass\n"
            "def on_frame(i, f, ctx):\n    return None\n"
            "def reading(ctx):\n    return {'ambient_trend_c': 1.0}\n"  # missing trend/confidence
        )
        recipe = json.loads((dst / "recipe.json").read_text())
        (dst / "recipe.json").write_text(json.dumps(sign_recipe(recipe, self.priv)))
        self.trust.add(self.pub, "test")
        reg = self.registry()
        self.assertEqual(reg.loaded, [])
        self.assertEqual([r.code for r in reg.rejected], [errors.DRYRUN_FAILED])

    def test_nondeterministic_transform_refused(self):
        # A transform with hidden counter state across on_frame is fine; one that
        # reads a MUTABLE GLOBAL that changes between runs is caught. Simulate the
        # simplest non-determinism: output depends on a module-level counter that
        # is never reset by init().
        dst = self.install_reference("thermal_ambient_proxy", sign=False, trust=False)
        (dst / "transform.py").write_text(
            "import itertools\n"
            "_c = itertools.count()\n"
            "def init(ctx):\n    pass\n"
            "def on_frame(i, f, ctx):\n    return None\n"
            "def reading(ctx):\n"
            "    return {'ambient_trend_c': float(next(_c)), 'trend': 'steady', 'confidence': 'low'}\n"
        )
        recipe = json.loads((dst / "recipe.json").read_text())
        (dst / "recipe.json").write_text(json.dumps(sign_recipe(recipe, self.priv)))
        self.trust.add(self.pub, "test")
        reg = self.registry()
        self.assertEqual([r.code for r in reg.rejected], [errors.DRYRUN_FAILED])
        self.assertIn("non-deterministic", reg.rejected[0].detail)


# ── contract check (fields / types / units / hz) ──────────────────────────────

class TestContract(DeriveBase):
    def _thermal_req(self):
        return {"capability_hint": "sensing",
                "fields": {"thermal.max_temp_c": {"type": "number", "unit": "C", "min_hz": 0.2}}}

    def test_real_sensing_manifest_satisfies_thermal(self):
        ok, reason = check_input_against_provider(self._thermal_req(), _sensing_manifest(), {})
        self.assertTrue(ok, reason)

    def test_missing_field_rejected(self):
        bad = {"description": "x", "reading": {"other": {"type": "number"}},
               "consent_tier": "open", "streaming": False}
        ok, reason = check_input_against_provider(self._thermal_req(), bad, {})
        self.assertFalse(ok)
        self.assertIn("missing field", reason)

    def test_unit_mismatch_without_adaptation_rejected(self):
        req = {"capability_hint": "demo_odometry",
               "fields": {"pose.x_m": {"type": "number", "unit": "m"},
                          "pose.y_m": {"type": "number", "unit": "m"}}}
        prov = _odometry_manifest(unit="cm")            # provider gives cm
        ok, reason = check_input_against_provider(req, prov, {})   # no adaptation declared
        self.assertFalse(ok)
        self.assertIn("unit mismatch", reason)

    def test_unit_mismatch_with_declared_adaptation_accepted(self):
        req = {"capability_hint": "demo_odometry",
               "fields": {"pose.x_m": {"type": "number", "unit": "m"},
                          "pose.y_m": {"type": "number", "unit": "m"}}}
        prov = _odometry_manifest(unit="cm")
        ok, reason = check_input_against_provider(req, prov, {"cm": "m"})
        self.assertTrue(ok, reason)

    def test_declared_but_unsupported_adaptation_rejected(self):
        # Declared cm->furlong, but the engine has no such scale -> match fails.
        req = {"capability_hint": "demo_odometry",
               "fields": {"pose.x_m": {"type": "number", "unit": "furlong"},
                          "pose.y_m": {"type": "number", "unit": "furlong"}}}
        prov = _odometry_manifest(unit="cm")
        ok, reason = check_input_against_provider(req, prov, {"cm": "furlong"})
        self.assertFalse(ok)

    def test_insufficient_hz_rejected(self):
        req = {"capability_hint": "sensing",
               "fields": {"thermal.max_temp_c":
                          {"type": "number", "unit": "C", "min_hz": DERIVE_MAX_INPUT_HZ + 5}}}
        ok, reason = check_input_against_provider(req, _sensing_manifest(), {})
        self.assertFalse(ok)
        self.assertIn("min_hz", reason)


# ── planner: direct-first, refusals, cost ranking, consent escalation ─────────

class TestPlanner(DeriveBase):
    def _planner(self, providers_by_name):
        reg = self.registry()
        return Planner(reg, discover=lambda n: providers_by_name.get(n, []))

    def test_direct_provider_beats_recipe(self):
        self.install_reference("thermal_ambient_proxy")
        # A REAL provider of 'ambient_temp' exists -> direct wins over the recipe.
        direct = _provider("nodeD", "ambient_temp", _sensing_manifest())
        pl = self._planner({"ambient_temp": [direct]})
        res = pl.need("ambient_temp")
        self.assertEqual(res.outcome, "direct")
        self.assertEqual(res.plan, None)
        self.assertEqual(res.direct_providers, [direct])

    def test_no_recipe_no_direct_refused(self):
        pl = self._planner({})
        res = pl.need("nonexistent_cap")
        self.assertEqual(res.outcome, "refused")
        self.assertEqual(res.code, errors.NO_RECIPE)

    def test_derive_when_no_direct_provider(self):
        self.install_reference("thermal_ambient_proxy")
        prov = _provider("nodeS", "sensing", _sensing_manifest())
        pl = self._planner({"sensing": [prov]})       # provides input, not the need directly
        res = pl.need("ambient_temp")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.recipe.recipe_name, "thermal_ambient_proxy")
        self.assertEqual(res.plan.provenance.inputs[0]["node_id"], "nodeS")

    def test_contract_unsatisfied_refused(self):
        self.install_reference("thermal_ambient_proxy")
        # provider exists for the input NAME but its manifest lacks the field
        bad = _provider("nodeX", "sensing",
                        {"description": "x", "reading": {"nope": {"type": "number"}},
                         "consent_tier": "open", "streaming": False})
        pl = self._planner({"sensing": [bad]})
        res = pl.need("ambient_temp")
        self.assertEqual(res.outcome, "refused")
        self.assertEqual(res.code, errors.CONTRACT_UNSATISFIED)

    def test_consent_escalation_open_inputs_to_sensitive_derived(self):
        self.install_reference("trajectory_free_space_map")
        odo = _provider("nodeO", "demo_odometry", _odometry_manifest("m"))  # OPEN input
        pl = self._planner({"demo_odometry": [odo]})
        res = pl.need("free_space_map")
        self.assertEqual(res.outcome, "derived")
        # inputs are open, recipe declared sensitive -> effective is provably max = sensitive
        self.assertEqual(res.plan.effective_tier, "sensitive")
        self.assertEqual(res.plan.manifest["consent_tier"], "sensitive")
        self.assertEqual(res.plan.provenance.effective_tier, "sensitive")

    def test_effective_tier_is_structural_max(self):
        self.assertEqual(effective_tier(["open", "open"]), "open")
        self.assertEqual(effective_tier(["open", "sensitive"]), "sensitive")
        self.assertEqual(effective_tier(["sensitive", "open"]), "sensitive")
        self.assertEqual(effective_tier(["open", "weird_unknown"]), "sensitive")  # fail-safe

    def test_cost_ranking_picks_richest_then_fewest_inputs(self):
        # Install thermal (cost 20) and a cheaper competitor (cost 5) that also
        # provides 'ambient_temp' from the same input -> cheaper (richer) wins.
        self.install_reference("thermal_ambient_proxy")
        rich = self.recipes_dir / "thermal_rich"
        shutil.copytree(_REF / "thermal_ambient_proxy", rich)
        recipe = json.loads((rich / "recipe.json").read_text())
        recipe["name"] = "thermal_ambient_proxy_rich"
        recipe["cost_rank_hint"] = 5
        (rich / "recipe.json").write_text(json.dumps(sign_recipe(recipe, self.priv)))
        prov = _provider("nodeS", "sensing", _sensing_manifest())
        pl = self._planner({"sensing": [prov]})
        res = pl.need("ambient_temp")
        self.assertEqual(res.outcome, "derived")
        self.assertEqual(res.plan.recipe.recipe_name, "thermal_ambient_proxy_rich")
        self.assertEqual(res.plan.cost_rank_hint, 5)

    def test_plan_carries_fidelity_and_cannot_detect(self):
        self.install_reference("trajectory_free_space_map")
        odo = _provider("nodeO", "demo_odometry", _odometry_manifest("m"))
        pl = self._planner({"demo_odometry": [odo]})
        res = pl.need("free_space_map")
        self.assertIn("obstacles", res.plan.cannot_detect)
        self.assertTrue(res.plan.fidelity)


# ── provenance completeness ────────────────────────────────────────────────────

class TestProvenance(DeriveBase):
    def test_provenance_records_full_lineage(self):
        self.install_reference("thermal_ambient_proxy")
        prov = _provider("nodeS", "sensing", _sensing_manifest())
        reg = self.registry()
        pl = Planner(reg, discover=lambda n: {"sensing": [prov]}.get(n, []))
        p = pl.need("ambient_temp").plan.provenance
        self.assertEqual(p.recipe, "thermal_ambient_proxy")
        self.assertEqual(p.version, "1.0.0")
        self.assertEqual(p.author_pubkey, self.pub)
        self.assertEqual(p.effective_tier, "open")
        self.assertEqual(p.inputs[0]["capability"], "sensing")
        self.assertEqual(p.inputs[0]["provider_name"], "sensing")


if __name__ == "__main__":
    unittest.main()
