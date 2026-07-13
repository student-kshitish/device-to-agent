"""
d2a_derive/registry.py — load, gate, validate, and admit recipe packages.

v1's registry is a LOCAL FOLDER (default <d2a_home>/recipes/). Community
distribution is future work; the format is already signed + self-contained so a
recipe can travel as a directory and be verified by its recipient.

ADMISSION PIPELINE for each package directory (order is load-bearing):

    1. FORMAT     RecipePackage.load — dir has recipe.json + transform.py +
                  test_frames.json, and recipe.json parses.        -> RECIPE_MALFORMED
    2. TRUST GATE (a) signature verifies vs embedded author_pubkey  -> RECIPE_UNSIGNED
                                                                       / RECIPE_BAD_SIG
                  (b) author_pubkey is in trusted_authors.json      -> RECIPE_UNTRUSTED_AUTHOR
       --- the trust gate is STRICTLY BEFORE any importlib below ---
    3. SCHEMA     validate_recipe_schema + validate_provides
                  (reuses d2a.manifest.validate_manifest)           -> RECIPE_INVALID
    4. DEPS       every declared dep imports, else the recipe is skipped (match-fail
                  precedent: a missing dep disqualifies, it does not crash)
    5. LOAD       load_transform (executes author code — now trusted)
    6. DRY-RUN    admission gate: transform must pass its own frames deterministically
                                                                     -> DRYRUN_FAILED

A recipe that clears all six is a LoadedRecipe, indexed by the capability name it
PROVIDES (provides.name) so the planner can cost-rank multiple providers of the
same name. Rejected packages are recorded (dir, code, detail) — never raised past
load_all — so one bad recipe cannot blind the registry to the good ones.
"""

import importlib
from dataclasses import dataclass, field
from pathlib import Path

from d2a import crypto
from d2a_derive import errors
from d2a_derive.metrics import MetricsStore
from d2a_derive.recipe import RecipePackage, verify_recipe_sig, is_signed, RecipeFormatError
from d2a_derive.trust import TrustStore
from d2a_derive.validator import validate_recipe_schema, validate_provides
from d2a_derive.loader import load_transform, TransformLoadError
from d2a_derive.dryrun import dry_run, DryRunResult


@dataclass
class LoadedRecipe:
    """A fully-admitted recipe: verified, validated, transform loaded, dry-run
    passed. This is what the planner consumes."""
    pkg: RecipePackage
    manifest: dict                       # validated provides manifest (vocab subset)
    meta: dict                           # {name, derived, recipe, fidelity, cannot_detect}
    module: object                       # loaded transform module
    dry_run: DryRunResult

    @property
    def provided_name(self) -> str:
        return self.meta["name"]

    @property
    def recipe_name(self) -> str:
        return self.pkg.name

    @property
    def version(self) -> str:
        return self.pkg.recipe.get("version", "")

    @property
    def author_pubkey(self) -> str:
        return self.pkg.author_pubkey

    @property
    def requires(self) -> list:
        return self.pkg.requires

    @property
    def unit_adaptations(self) -> dict:
        return self.pkg.unit_adaptations

    @property
    def cost_rank_hint(self) -> int:
        return self.pkg.cost_rank_hint

    @property
    def fidelity(self) -> str:
        return self.meta.get("fidelity", "")

    @property
    def cannot_detect(self) -> list:
        return self.meta.get("cannot_detect", [])


@dataclass
class RecipeError:
    """A recorded rejection (not an exception — collected, not raised)."""
    dir: str
    code: str
    detail: str = ""


class Registry:
    def __init__(self, recipes_dir=None, trust: TrustStore | None = None,
                 auto_load: bool = True):
        self.recipes_dir = Path(recipes_dir) if recipes_dir is not None \
            else (crypto.d2a_home() / "recipes")
        self.trust = trust if trust is not None else TrustStore()
        self._by_provided: dict[str, list[LoadedRecipe]] = {}
        self.loaded: list[LoadedRecipe] = []
        self.rejected: list[RecipeError] = []
        if auto_load:
            self.load_all()

    # ── loading ──────────────────────────────────────────────────────────────

    def load_all(self) -> None:
        """(Re)scan the recipes folder. Each admitted recipe joins the index; each
        rejection is recorded in self.rejected."""
        self._by_provided = {}
        self.loaded = []
        self.rejected = []
        if not self.recipes_dir.is_dir():
            return
        for child in sorted(self.recipes_dir.iterdir()):
            if not child.is_dir():
                continue
            try:
                lr = self.load_one(child)
            except errors.DeriveError as exc:
                self.rejected.append(RecipeError(str(child), exc.code, exc.detail))
                continue
            self._index(lr)

    def load_one(self, directory) -> LoadedRecipe:
        """Run the full admission pipeline for one package dir. Raises DeriveError
        with a distinct code at whichever gate fails."""
        # 1. FORMAT
        try:
            pkg = RecipePackage.load(directory)
        except RecipeFormatError as exc:
            raise errors.DeriveError(errors.RECIPE_MALFORMED, str(exc)) from exc

        # 2. TRUST GATE — strictly before any importlib.
        if not is_signed(pkg.recipe):
            raise errors.DeriveError(errors.RECIPE_UNSIGNED,
                                     f"{directory}: recipe is not signed")
        if not verify_recipe_sig(pkg.recipe):
            raise errors.DeriveError(errors.RECIPE_BAD_SIG,
                                     f"{directory}: signature does not verify against author_pubkey")
        if not self.trust.is_trusted(pkg.author_pubkey):
            raise errors.DeriveError(errors.RECIPE_UNTRUSTED_AUTHOR,
                                     f"{directory}: author {pkg.author_pubkey[:16]}… not in "
                                     f"trusted_authors.json (review-then-trust to install)")

        # 3. SCHEMA + provides manifest (reuses d2a.manifest.validate_manifest).
        validate_recipe_schema(pkg.recipe)
        manifest, meta = validate_provides(pkg.provides)

        # 4. DEPS — a declared dep that will not import disqualifies the recipe
        #    (match-fail precedent), rather than crashing later at transform time.
        for dep in pkg.deps:
            if not _dep_importable(dep):
                raise errors.DeriveError(
                    errors.RECIPE_INVALID,
                    f"declared dep {dep!r} not importable — recipe disqualified")

        # 5. LOAD transform (executes author code — trust gate has passed).
        try:
            module = load_transform(pkg)
        except TransformLoadError as exc:
            raise errors.DeriveError(errors.RECIPE_INVALID, str(exc)) from exc

        # 6. DRY-RUN admission gate.
        dr = dry_run(pkg, module, manifest)
        if not dr.ok:
            raise errors.DeriveError(errors.DRYRUN_FAILED, dr.reason)

        return LoadedRecipe(pkg=pkg, manifest=manifest, meta=meta, module=module, dry_run=dr)

    def _index(self, lr: LoadedRecipe) -> None:
        self.loaded.append(lr)
        self._by_provided.setdefault(lr.provided_name, []).append(lr)

    # ── query ──────────────────────────────────────────────────────────────────

    def recipes_for(self, provided_name: str) -> list[LoadedRecipe]:
        """All admitted recipes that provide `provided_name` (planner cost-ranks)."""
        return list(self._by_provided.get(provided_name, []))

    def provided_names(self) -> list[str]:
        return sorted(self._by_provided)


def _dep_importable(dep: str) -> bool:
    try:
        importlib.import_module(dep)
        return True
    except Exception:                            # noqa: BLE001
        return False


# ── registry hygiene: list / show (Phase 5) ──────────────────────────────────

def _fingerprint(pubkey: str) -> str:
    """Short author fingerprint (first 16 hex + ellipsis) — the width the gate
    messages already use, so an author is recognisable across the whole toolchain."""
    return (pubkey[:16] + "…") if pubkey else "(none)"


def _raw_summary(child: Path, trust: TrustStore) -> dict:
    """A hygiene summary of one package DIR read raw (parsed, not admitted, never
    executed) so a rejected/untrusted package still shows up in `list`/`show`."""
    try:
        pkg = RecipePackage.load(child)
    except RecipeFormatError as exc:
        return {"dir": child.name, "malformed": str(exc), "name": child.name}
    r = pkg.recipe
    provides = r.get("provides") if isinstance(r.get("provides"), dict) else {}
    return {
        "dir":         child.name,
        "name":        r.get("name", ""),
        "version":     r.get("version", ""),
        "provides":    provides.get("name", ""),
        "author":      pkg.author_pubkey,
        "fingerprint": _fingerprint(pkg.author_pubkey),
        "trusted":     trust.is_trusted(pkg.author_pubkey),
        "tier":        provides.get("consent_tier", ""),
        "requires":    [req.get("capability_hint") for req in (r.get("requires") or [])
                        if isinstance(req, dict)],
        "fidelity":    provides.get("fidelity", ""),
        "cannot_detect": provides.get("cannot_detect", []),
    }


def summarize(recipes_dir, trust: TrustStore | None = None,
              metrics: MetricsStore | None = None) -> list[dict]:
    """Summarize every package in `recipes_dir`, annotated with admission status
    (admitted, or rejected + code) from a real Registry load, and — Phase 6 — the
    recipe's observed-runtime QUARANTINE flag + lifetime metrics from `metrics`.
    Returns one dict per package dir, sorted by name."""
    recipes_dir = Path(recipes_dir)
    trust = trust if trust is not None else TrustStore()
    metrics = metrics if metrics is not None else MetricsStore()
    reg = Registry(recipes_dir=recipes_dir, trust=trust)   # runs the full pipeline
    admitted = {lr.recipe_name for lr in reg.loaded}
    rejected = {Path(re.dir).name: re.code for re in reg.rejected}

    out = []
    if recipes_dir.is_dir():
        for child in sorted(recipes_dir.iterdir()):
            if not child.is_dir():
                continue
            s = _raw_summary(child, trust)
            if s.get("name") in admitted and "malformed" not in s:
                s["status"] = "admitted"
            else:
                s["status"] = "rejected"
                s["reject_code"] = rejected.get(child.name, "unknown")
            # metrics are keyed by recipe_name (== recipe.json "name"). Never silent:
            # a quarantined recipe is flagged here and requires an explicit opt-in to
            # be planned (see planner + explain).
            rname = s.get("name") or ""
            s["quarantined"] = metrics.is_quarantined(rname)
            s["metrics"] = metrics.get(rname).summary()
            out.append(s)
    return out


def _cmd_list(recipes_dir, trust, metrics=None) -> int:
    rows = summarize(recipes_dir, trust, metrics)
    if not rows:
        print(f"(no recipe packages in {Path(recipes_dir)})")
        return 0
    print(f"{'NAME':<26} {'VERSION':<9} {'TIER':<10} {'TRUST':<8} "
          f"{'STATUS':<10} {'QUAR':<6} {'AUTHOR':<18} REQUIRES")
    for s in rows:
        if "malformed" in s:
            print(f"{s['dir']:<26} {'-':<9} {'-':<10} {'-':<8} {'malformed':<10} "
                  f"{'-':<6} {'-':<18} {s['malformed']}")
            continue
        trust_s = "trusted" if s["trusted"] else "UNTRUSTED"
        status = s["status"] if s["status"] == "admitted" else f"× {s.get('reject_code','')}"
        quar = "QUAR" if s.get("quarantined") else "-"
        print(f"{s['name']:<26} {s['version']:<9} {s['tier']:<10} {trust_s:<8} "
              f"{status:<10} {quar:<6} {s['fingerprint']:<18} {', '.join(s['requires'])}")
    return 0


def _cmd_show(recipes_dir, trust, name: str, metrics=None) -> int:
    recipes_dir = Path(recipes_dir)
    child = recipes_dir / name
    if not child.is_dir():
        print(f"no such recipe package: {name} (in {recipes_dir})")
        return 1
    rows = {s["dir"]: s for s in summarize(recipes_dir, trust, metrics)}
    s = rows.get(name) or _raw_summary(child, trust)
    if "malformed" in s:
        print(f"{name}: MALFORMED — {s['malformed']}")
        return 1
    print(f"recipe package : {s['name']}  (dir {s['dir']})")
    print(f"  version      : {s['version']}")
    print(f"  provides     : {s['provides']}")
    print(f"  author       : {s['author']}")
    print(f"  fingerprint  : {s['fingerprint']}")
    print(f"  trusted      : {'yes' if s['trusted'] else 'NO'}")
    print(f"  consent tier : {s['tier']}")
    print(f"  requires     : {', '.join(s['requires']) or '(none)'}")
    print(f"  status       : {s.get('status', '?')}"
          + (f"  ({s['reject_code']})" if s.get("status") == "rejected" else ""))
    print(f"  fidelity     : {s['fidelity']}")
    print(f"  cannot_detect: {len(s['cannot_detect'])} blind spot(s)")
    for c in s["cannot_detect"]:
        print(f"    · {c}")
    # Phase 6: observed-runtime record on THIS machine (advisory — informs planning).
    m = s.get("metrics") or {}
    print(f"  quarantined  : {'YES — needs --include-quarantined / re-run conformance to clear' if s.get('quarantined') else 'no'}")
    print(f"  observed     : runs={m.get('runs', 0)} "
          f"failure_rate={m.get('failure_rate', 0.0)} heal_rate={m.get('heal_rate', 0.0)} "
          f"mean_staleness_s={m.get('mean_staleness_s', 0.0)}")
    lc = m.get("last_conformance")
    print(f"  conformance  : {('passed' if lc.get('passed') else 'FAILED') if lc else '(never run)'}")
    return 0


def main(argv=None) -> int:
    import argparse
    from d2a import crypto

    ap = argparse.ArgumentParser(
        prog="python -m d2a_derive.registry",
        description="Inspect the local recipe registry (hygiene).")
    ap.add_argument("--recipes-dir", default=None,
                    help="registry dir (default <d2a_home>/recipes)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list every installed package with trust + status")
    sp = sub.add_parser("show", help="show one package in detail")
    sp.add_argument("name")
    args = ap.parse_args(argv)

    recipes_dir = args.recipes_dir if args.recipes_dir is not None \
        else (crypto.d2a_home() / "recipes")
    trust = TrustStore()
    metrics = MetricsStore()
    if args.cmd == "list":
        return _cmd_list(recipes_dir, trust, metrics)
    return _cmd_show(recipes_dir, trust, args.name, metrics)


if __name__ == "__main__":
    raise SystemExit(main())
