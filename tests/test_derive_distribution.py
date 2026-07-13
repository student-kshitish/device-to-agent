"""
tests/test_derive_distribution.py — Phase 5 of CASE 4 (community distribution).

Covers the layer that turns the local recipe registry into COMMUNITY infrastructure:

  * remote.py   — fetch a package from a DIRECTORY source and from a URL source
                  (an in-test http.server), and refuse a malformed/missing package.
  * install.py  — the review-then-trust install: refuse bad-sig / untrusted-without-
                  confirmation / duplicate-version; install on typed confirmation and
                  land BOTH the package and a new trust entry; treat a bumped version
                  as an upgrade that RE-REVIEWS (a new version is new code).
  * sign.py     — the self-check gate: refuse to sign a package missing its honesty
                  fields or failing its own dry-run; sign a good package.
  * new.py      — scaffold a package with every mandatory honesty field present.
  * conformance.py — pass for a shipped recipe (dry-run + bounded live), fail for a
                  deliberately broken fixture (nondeterministic ACROSS reloads — the
                  exact thing a single dry-run cannot catch but conformance does).

Hermetic: each test signs into a TEMP recipes dir with a freshly-generated author
key and points a TrustStore at a temp trusted_authors.json. D2A_HOME is redirected
so the sign CLI's keystore and any --trust write never touch the real home.
"""

import http.server
import json
import os
import shutil
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from d2a import crypto
from d2a_derive import (
    TrustStore, sign_recipe, errors,
    open_source, DirectorySource, UrlSource, RemoteSourceError,
    run_install, run_conformance,
)
from d2a_derive.recipe import RECIPE_JSON, TRANSFORM_PY, TEST_FRAMES_JSON, RecipePackage
from d2a_derive import install as install_mod
from d2a_derive import new as new_mod
from d2a_derive import sign as sign_mod
from d2a_derive.sign import sign_recipe_dir, check_signable, RefuseToSign

_REPO = Path(__file__).resolve().parent.parent
_REF = _REPO / "d2a_derive" / "reference_recipes"


# ── a deliberately broken recipe: passes a single dry-run, fails across reloads ──

# Determinism holds WITHIN one module load (a fixed import-time seed), so the sign
# gate and the registry admission dry-run (both same-module) accept it — but the
# seed differs on a FRESH import, so conformance's twice-across-reloads check trips.
_NONDET_TRANSFORM = '''\
"""A recipe that is deterministic within a load but NOT across reloads."""
import random
_SEED = random.random()          # import-time randomness → stable per load, differs per reload


def init(ctx):
    ctx["v"] = 0.0


def on_frame(input_name, frame, ctx):
    fields = frame.get("fields", {})
    if "cpu.util_pct" in fields:
        ctx["v"] = float(fields["cpu.util_pct"]) + _SEED
    return reading(ctx)


def reading(ctx):
    return {"value": round(ctx.get("v", 0.0), 6)}
'''

_NONDET_FRAMES = [
    {"input": "compute", "fields": {"cpu.util_pct": 10.0}, "ts": 1.0, "seq": 1},
    {"input": "compute", "fields": {"cpu.util_pct": 20.0}, "ts": 2.0, "seq": 2},
]

_NONDET_RECIPE = {
    "name": "flaky",
    "version": "0.1.0",
    "author_pubkey": "",
    "sig": "",
    "cost_rank_hint": 0,
    "unit_adaptations": {},
    "requires": [{"capability_hint": "compute",
                  "fields": {"cpu.util_pct": {"type": "number", "min_hz": 0.5}}}],
    "provides": {
        "name": "flaky", "derived": True, "recipe": "flaky",
        "fidelity": "coarse proxy for testing only",
        "cannot_detect": ["anything real"],
        "description": "a deliberately flaky fixture",
        "consent_tier": "open",
        "reading": {"value": {"type": "number", "description": "seeded value"}},
        "streaming": False,
    },
}


class DistBase(unittest.TestCase):
    def setUp(self):
        self._home = TemporaryDirectory()
        os.environ["D2A_HOME"] = self._home.name
        self.home = Path(self._home.name)
        self.recipes_dir = self.home / "recipes"
        self.recipes_dir.mkdir(parents=True, exist_ok=True)
        self.priv, self.pub = crypto.generate_keypair()
        self.trust = TrustStore(path=self.home / "trusted_authors.json")

    def tearDown(self):
        os.environ.pop("D2A_HOME", None)
        self._home.cleanup()

    # helpers ------------------------------------------------------------------

    def make_source_pkg(self, base: Path, ref_name: str, *, priv=None,
                        version=None, dest_name=None) -> Path:
        """Copy a shipped reference recipe into a SOURCE layout <base>/<name>/, sign
        it (optionally bumping the version), and return the package dir."""
        priv = priv if priv is not None else self.priv
        name = dest_name or ref_name
        dst = base / name
        shutil.copytree(_REF / ref_name, dst)
        recipe = json.loads((dst / RECIPE_JSON).read_text())
        if version is not None:
            recipe["version"] = version
        (dst / RECIPE_JSON).write_text(json.dumps(sign_recipe(recipe, priv), indent=2))
        return dst

    def write_pkg(self, base: Path, name: str, recipe: dict, transform: str,
                  frames: list, *, priv=None) -> Path:
        """Write + sign a hand-built package into <base>/<name>/."""
        priv = priv if priv is not None else self.priv
        d = base / name
        d.mkdir(parents=True)
        (d / TRANSFORM_PY).write_text(transform)
        (d / TEST_FRAMES_JSON).write_text(json.dumps(frames, indent=2))
        (d / RECIPE_JSON).write_text(json.dumps(sign_recipe(recipe, priv), indent=2))
        return d


# ── remote.py — DIRECTORY source ──────────────────────────────────────────────

class TestDirectorySource(DistBase):
    def test_open_source_picks_directory_for_plain_path(self):
        self.assertIsInstance(open_source("/some/local/path"), DirectorySource)
        self.assertIsInstance(open_source("./relative"), DirectorySource)

    def test_fetch_from_directory(self):
        with TemporaryDirectory() as src, TemporaryDirectory() as stage:
            self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            source = open_source(src)
            dest = source.fetch("thermal_ambient_proxy", stage)
            for fn in (RECIPE_JSON, TRANSFORM_PY, TEST_FRAMES_JSON):
                self.assertTrue((dest / fn).is_file(), fn)
            # the fetched package parses as a real RecipePackage
            RecipePackage.load(dest)

    def test_directory_available_lists_packages(self):
        with TemporaryDirectory() as src:
            self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            self.make_source_pkg(Path(src), "load_trend_from_thermal")
            names = DirectorySource(src).available()
            self.assertEqual(names, ["load_trend_from_thermal", "thermal_ambient_proxy"])

    def test_fetch_missing_package_raises(self):
        with TemporaryDirectory() as src, TemporaryDirectory() as stage:
            with self.assertRaises(RemoteSourceError):
                open_source(src).fetch("does_not_exist", stage)

    def test_fetch_missing_leaf_file_raises(self):
        with TemporaryDirectory() as src, TemporaryDirectory() as stage:
            d = self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            (d / TRANSFORM_PY).unlink()          # a package without its transform
            with self.assertRaises(RemoteSourceError):
                open_source(src).fetch("thermal_ambient_proxy", stage)

    def test_rejects_unsupported_scheme(self):
        with self.assertRaises(RemoteSourceError):
            open_source("git://example.com/recipes")


# ── remote.py — URL source (in-test http.server) ──────────────────────────────

class TestUrlSource(DistBase):
    def _serve(self, root: Path):
        """Serve `root` over http on an ephemeral port; return (base_url, stop)."""
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(
            *a, directory=str(root), **k)
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        host, port = httpd.server_address
        base = f"http://{host}:{port}"

        def stop():
            httpd.shutdown()
            t.join(timeout=2)
        return base, stop

    def test_open_source_picks_url(self):
        self.assertIsInstance(open_source("http://x/recipes"), UrlSource)
        self.assertIsInstance(open_source("https://x/recipes"), UrlSource)

    def test_fetch_from_url(self):
        with TemporaryDirectory() as src, TemporaryDirectory() as stage:
            self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            base, stop = self._serve(Path(src))
            try:
                dest = open_source(base).fetch("thermal_ambient_proxy", stage)
                for fn in (RECIPE_JSON, TRANSFORM_PY, TEST_FRAMES_JSON):
                    self.assertTrue((dest / fn).is_file(), fn)
                RecipePackage.load(dest)
            finally:
                stop()

    def test_fetch_from_url_missing_is_error(self):
        with TemporaryDirectory() as src, TemporaryDirectory() as stage:
            base, stop = self._serve(Path(src))
            try:
                with self.assertRaises(RemoteSourceError):
                    open_source(base).fetch("nope", stage)
            finally:
                stop()


# ── install.py — the review-then-trust flow ───────────────────────────────────

class TestInstall(DistBase):
    def _install(self, src, name, confirm=None, assume_reviewed=False):
        return run_install(src, name, recipes_dir=self.recipes_dir, trust=self.trust,
                           confirm=confirm, assume_reviewed=assume_reviewed,
                           out=lambda *a, **k: None)

    def test_install_new_author_lands_package_and_trust(self):
        with TemporaryDirectory() as src:
            self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            res = self._install(src, "thermal_ambient_proxy", confirm=lambda: "author-trust")
            self.assertTrue(res.installed, res.reason)
            self.assertTrue(res.author_added)
            self.assertTrue((self.recipes_dir / "thermal_ambient_proxy" / RECIPE_JSON).is_file())
            self.assertTrue(TrustStore(path=self.home / "trusted_authors.json").is_trusted(self.pub))

    def test_new_author_needs_strong_confirmation(self):
        # a NEW (untrusted) author is NOT installed on a bare 'yes' — needs author-trust
        with TemporaryDirectory() as src:
            self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            res = self._install(src, "thermal_ambient_proxy", confirm=lambda: "yes")
            self.assertFalse(res.installed)
            self.assertFalse(self.trust.is_trusted(self.pub))

    def test_install_declined(self):
        with TemporaryDirectory() as src:
            self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            res = self._install(src, "thermal_ambient_proxy", confirm=lambda: "no")
            self.assertFalse(res.installed)
            self.assertFalse((self.recipes_dir / "thermal_ambient_proxy").exists())

    def test_install_refuses_bad_signature(self):
        with TemporaryDirectory() as src:
            d = self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            recipe = json.loads((d / RECIPE_JSON).read_text())
            recipe["cost_rank_hint"] = recipe.get("cost_rank_hint", 0) + 1  # tamper signed field
            (d / RECIPE_JSON).write_text(json.dumps(recipe))
            res = self._install(src, "thermal_ambient_proxy", confirm=lambda: "author-trust")
            self.assertFalse(res.installed)
            self.assertEqual(res.code, errors.RECIPE_BAD_SIG)

    def test_install_refuses_fetch_failure(self):
        with TemporaryDirectory() as src:
            res = self._install(src, "not_there", confirm=lambda: "author-trust")
            self.assertFalse(res.installed)
            self.assertEqual(res.code, errors.RECIPE_FETCH_FAILED)

    def test_install_refuses_duplicate_version(self):
        with TemporaryDirectory() as src:
            self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            first = self._install(src, "thermal_ambient_proxy", confirm=lambda: "author-trust")
            self.assertTrue(first.installed)
            # same name+version again → duplicate refusal (author now trusted, still refused)
            again = self._install(src, "thermal_ambient_proxy", confirm=lambda: "yes")
            self.assertFalse(again.installed)
            self.assertEqual(again.code, errors.RECIPE_DUPLICATE)

    def test_upgrade_requires_re_review(self):
        with TemporaryDirectory() as src1, TemporaryDirectory() as src2:
            self.make_source_pkg(Path(src1), "thermal_ambient_proxy")
            first = self._install(src1, "thermal_ambient_proxy", confirm=lambda: "author-trust")
            self.assertTrue(first.installed)
            # a bumped version is new code: it re-runs the review and can be DECLINED
            self.make_source_pkg(Path(src2), "thermal_ambient_proxy", version="9.9.9")
            declined = self._install(src2, "thermal_ambient_proxy", confirm=lambda: "no")
            self.assertFalse(declined.installed)
            # still the OLD version on disk
            on_disk = json.loads(
                (self.recipes_dir / "thermal_ambient_proxy" / RECIPE_JSON).read_text())
            self.assertNotEqual(on_disk["version"], "9.9.9")
            # confirm the re-review → upgrade lands, reports upgraded_from
            ok = self._install(src2, "thermal_ambient_proxy", confirm=lambda: "yes")
            self.assertTrue(ok.installed, ok.reason)
            self.assertTrue(ok.upgraded_from)
            upgraded = json.loads(
                (self.recipes_dir / "thermal_ambient_proxy" / RECIPE_JSON).read_text())
            self.assertEqual(upgraded["version"], "9.9.9")

    def test_yes_i_reviewed_is_non_interactive(self):
        with TemporaryDirectory() as src:
            self.make_source_pkg(Path(src), "thermal_ambient_proxy")
            # no confirm callable at all; the flag stands in for the strongest token
            res = self._install(src, "thermal_ambient_proxy", assume_reviewed=True)
            self.assertTrue(res.installed, res.reason)
            self.assertTrue(res.author_added)


# ── sign.py — the self-check gate ─────────────────────────────────────────────

class TestSignSelfCheck(DistBase):
    def test_refuses_missing_honesty_fields(self):
        # a freshly-scaffolded package has fidelity="" / cannot_detect=[] by design
        with TemporaryDirectory() as work:
            new_mod.scaffold("myrec", work)
            with self.assertRaises(RefuseToSign) as cm:
                sign_recipe_dir(Path(work) / "myrec", "authorkey")
            self.assertIn("fidelity", str(cm.exception).lower())

    def test_refuses_failing_dry_run(self):
        # honesty fields filled, but the transform emits fields the manifest never declared
        recipe = {
            "name": "bad", "version": "0.1.0", "author_pubkey": "", "sig": "",
            "cost_rank_hint": 0, "unit_adaptations": {},
            "requires": [{"capability_hint": "compute",
                          "fields": {"cpu.util_pct": {"type": "number", "min_hz": 0.5}}}],
            "provides": {
                "name": "bad", "derived": True, "recipe": "bad",
                "fidelity": "stated", "cannot_detect": ["a blind spot"],
                "description": "x", "consent_tier": "open",
                "reading": {"value": {"type": "number", "description": "v"}},
                "streaming": False,
            },
        }
        transform = (
            "def init(ctx):\n    ctx['v'] = 0.0\n\n"
            "def on_frame(n, f, ctx):\n    return reading(ctx)\n\n"
            "def reading(ctx):\n    return {'WRONG': 1.0}\n"   # declared field is 'value'
        )
        with TemporaryDirectory() as work:
            d = Path(work) / "bad"
            d.mkdir()
            (d / RECIPE_JSON).write_text(json.dumps(recipe))
            (d / TRANSFORM_PY).write_text(transform)
            (d / TEST_FRAMES_JSON).write_text(json.dumps(_NONDET_FRAMES))
            with self.assertRaises(RefuseToSign) as cm:
                check_signable(RecipePackage.load(d))
            self.assertIn("dry-run", str(cm.exception).lower())

    def test_signs_good_package(self):
        with TemporaryDirectory() as work:
            pkg_dir = new_mod.scaffold("goodrec", work)
            # fill the honesty fields the scaffold deliberately left empty
            recipe = json.loads((pkg_dir / RECIPE_JSON).read_text())
            recipe["provides"]["fidelity"] = "coarse CPU echo, demonstration only"
            recipe["provides"]["cannot_detect"] = ["actual workload semantics"]
            (pkg_dir / RECIPE_JSON).write_text(json.dumps(recipe, indent=2))
            signed = sign_recipe_dir(pkg_dir, "goodkey")
            self.assertTrue(signed.get("sig"))
            from d2a_derive import verify_recipe_sig
            self.assertTrue(verify_recipe_sig(
                json.loads((pkg_dir / RECIPE_JSON).read_text())))


# ── new.py — scaffolding ──────────────────────────────────────────────────────

class TestScaffold(DistBase):
    def test_scaffold_writes_all_files_with_empty_honesty(self):
        with TemporaryDirectory() as work:
            pkg_dir = new_mod.scaffold("fresh", work)
            for fn in (RECIPE_JSON, TRANSFORM_PY, TEST_FRAMES_JSON):
                self.assertTrue((pkg_dir / fn).is_file(), fn)
            recipe = json.loads((pkg_dir / RECIPE_JSON).read_text())
            self.assertEqual(recipe["provides"]["fidelity"], "")
            self.assertEqual(recipe["provides"]["cannot_detect"], [])

    def test_scaffold_refuses_overwrite(self):
        with TemporaryDirectory() as work:
            new_mod.scaffold("dup", work)
            with self.assertRaises(FileExistsError):
                new_mod.scaffold("dup", work)


# ── conformance.py — the artifact ─────────────────────────────────────────────

class TestConformance(DistBase):
    def _install_ref(self, name, version=None):
        """Copy + sign a reference recipe straight into the registry dir and trust it."""
        dst = self.recipes_dir / name
        shutil.copytree(_REF / name, dst)
        recipe = json.loads((dst / RECIPE_JSON).read_text())
        if version is not None:
            recipe["version"] = version
        (dst / RECIPE_JSON).write_text(json.dumps(sign_recipe(recipe, self.priv), indent=2))
        self.trust.add(self.pub, "test")
        return dst

    def test_conformance_dryrun_only_passes_for_shipped(self):
        self._install_ref("thermal_ambient_proxy")
        report = run_conformance("thermal_ambient_proxy", recipes_dir=self.recipes_dir,
                                 trust=self.trust, live=False)
        self.assertTrue(report["dry_run"]["ok"], report["dry_run"])
        self.assertEqual(report["dry_run"]["runs"], 2)
        self.assertFalse(report["live"]["ran"])
        self.assertTrue(report["passed"])

    def test_conformance_full_passes_for_shipped(self):
        # thermal_ambient_proxy requires 'sensing', which the live harness stands up.
        self._install_ref("thermal_ambient_proxy")
        report = run_conformance("thermal_ambient_proxy", recipes_dir=self.recipes_dir,
                                 trust=self.trust, live=True, live_seconds=1.5)
        self.assertTrue(report["dry_run"]["ok"], report["dry_run"])
        self.assertTrue(report["live"]["ran"], report["live"])
        self.assertTrue(report["live"]["ok"], report["live"])
        self.assertTrue(report["passed"], report)

    def test_conformance_fails_broken_fixture(self):
        # deterministic within a load, NOT across reloads → dry-run admits it, but the
        # twice-across-reloads conformance check catches it.
        self.write_pkg(self.recipes_dir, "flaky", _NONDET_RECIPE,
                       _NONDET_TRANSFORM, _NONDET_FRAMES)
        self.trust.add(self.pub, "test")
        report = run_conformance("flaky", recipes_dir=self.recipes_dir,
                                 trust=self.trust, live=False)
        self.assertFalse(report["passed"], report)
        self.assertFalse(report["dry_run"]["ok"])
        self.assertIn("nondeterministic", report["dry_run"]["reason"].lower())

    def test_conformance_missing_recipe(self):
        report = run_conformance("ghost", recipes_dir=self.recipes_dir,
                                 trust=self.trust, live=False)
        self.assertFalse(report["passed"])


if __name__ == "__main__":
    unittest.main()
