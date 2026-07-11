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
