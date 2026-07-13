"""
d2a_derive/install.py — the review-then-trust install step, made mechanical.

    python -m d2a_derive.install <source> <name> [--yes-i-reviewed]

Trust v1 has always been "a human reviews a recipe and its author, then adds the
key" (trust.py). Phase 5 turns that prose into a MECHANICAL, auditable flow — but
does NOT change what trust MEANS. The security model is unchanged and stated
plainly: LOADING A TRANSFORM IS EXECUTING IT; there is no sandbox; the review IS
the security boundary. This tool refuses to make that boundary invisible.

FLOW (each step gates the next):
  1. FETCH   the package from <source> (dir or URL) into a throwaway STAGING dir.
             Fetching never executes anything (remote.py).
  2. FORMAT  the staged package parses (recipe.json + transform.py + test_frames).
  3. SIGN    the signature verifies against the recipe's EMBEDDED author_pubkey
             (authorship). A bad/absent signature is a HARD refusal — no
             confirmation can override a package whose author bytes don't check.
  4. SCHEMA  the recipe envelope + provides manifest validate (a malformed recipe
             is refused before we ever offer to trust it).
  5. DUPLICATE guard: an identical name+version already installed is refused
             (RECIPE_DUPLICATE). A DIFFERENT version is an UPGRADE — allowed, but it
             re-runs this whole review (a new version is new code).
  6. REVIEW  print author fingerprint, requires/provides, effective-tier
             implications, fidelity, cannot_detect, AND THE FULL transform.py to the
             terminal. This is the human's chance to read the code that is about to
             become executable on their machine.
  7. CONFIRM require an explicit typed token. A NEW author (not yet trusted) demands
             the stronger `author-trust` — installing their recipe extends trust to
             their key for ALL their future recipes, and you must say so. An
             already-trusted author accepts `yes` or `author-trust`. `--yes-i-reviewed`
             is the non-interactive escape hatch, named to be un-typo-able in a
             script and to state what it asserts.
  8. INSTALL copy the staged package into the registry dir and, if the author is
             new, add their pubkey to trusted_authors.json. Only now — after the
             human's explicit go — does the recipe become loadable (and executable).
"""

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from d2a import crypto
from d2a_derive import errors
from d2a_derive.recipe import (
    RECIPE_JSON, TRANSFORM_PY, RecipePackage, RecipeFormatError,
    verify_recipe_sig, is_signed,
)
from d2a_derive.remote import open_source, RemoteSourceError
from d2a_derive.trust import TrustStore
from d2a_derive.validator import validate_recipe_schema, validate_provides

# Accepted typed confirmations. `author-trust` is the stronger phrase required when
# the author is not yet trusted (installing extends trust to a NEW key); `yes`
# suffices only when the author is already trusted (e.g. a version upgrade).
CONFIRM_YES = "yes"
CONFIRM_AUTHOR_TRUST = "author-trust"


@dataclass
class InstallResult:
    """Structured outcome of run_install (returned, not raised, so control flow is
    testable). `installed` is the single success signal; `code` carries a distinct
    derive error code on a hard refusal; `reason` is human detail."""
    installed: bool
    name: str
    version: str = ""
    author_pubkey: str = ""
    code: str = ""
    reason: str = ""
    upgraded_from: str = ""          # previous version, when this was an upgrade
    author_added: bool = False       # did we add a NEW author to the trust store?


def fingerprint(pubkey: str) -> str:
    """A short, human-comparable author fingerprint (first 16 hex + ellipsis). The
    same 16-char width the registry gate messages already use."""
    return (pubkey[:16] + "…") if pubkey else "(none)"


def _installed_version(recipes_dir: Path, name: str) -> str | None:
    """The version of an already-installed package of this name, or None if not
    installed. Best-effort: an unparseable existing recipe.json reads as version ''."""
    rj = recipes_dir / name / RECIPE_JSON
    if not rj.is_file():
        return None
    try:
        return str(json.loads(rj.read_text()).get("version", ""))
    except (ValueError, OSError):
        return ""


def _print_review(out, pkg: RecipePackage, meta: dict, manifest: dict,
                  trusted: bool, upgraded_from: str | None) -> None:
    """Print the full review to `out`. This is the security boundary: everything a
    human needs to decide, including the transform source, in one screen."""
    recipe = pkg.recipe
    out("")
    out("=" * 72)
    out(f"REVIEW recipe package : {pkg.name}  (version {recipe.get('version', '?')})")
    out("=" * 72)
    out(f"  author pubkey   : {recipe.get('author_pubkey', '')}")
    out(f"  fingerprint     : {fingerprint(recipe.get('author_pubkey', ''))}")
    out(f"  author trusted  : {'YES (already in trusted_authors.json)' if trusted else 'NO — NEW author'}")
    if upgraded_from is not None:
        out(f"  UPGRADE         : replaces installed version {upgraded_from or '(unknown)'} "
            f"— a new version is new code, re-review below")

    # requires / provides summary
    out("")
    out(f"  provides        : {meta['name']}  (derived capability)")
    reading = manifest.get("reading", {})
    out(f"    reading fields: {', '.join(sorted(reading)) or '(none)'}")
    reqs = recipe.get("requires", [])
    out(f"  requires        : {len(reqs)} input(s)")
    for req in reqs:
        hint = req.get("capability_hint", "?")
        fields = ", ".join(sorted(req.get("fields", {})))
        opt = " [optional]" if req.get("optional") else ""
        out(f"    ← {hint}{opt}: {fields}")

    # effective-tier implications
    declared = manifest.get("consent_tier", "?")
    out("")
    out(f"  declared tier   : {declared}")
    out("  effective tier  : max(declared, ALL input tiers) — computed at bind time.")
    if declared == "sensitive":
        out("                    this recipe is SENSITIVE regardless of input tiers.")
    else:
        out("                    a SENSITIVE input escalates the derived capability to sensitive.")

    # honesty fields
    out("")
    out(f"  fidelity        : {meta.get('fidelity', '')}")
    cd = meta.get("cannot_detect", [])
    out(f"  cannot_detect   : {len(cd)} declared blind spot(s)")
    for c in cd:
        out(f"    · {c}")

    # THE FULL TRANSFORM — loading is executing; read it before you trust it.
    out("")
    out("-" * 72)
    out(f"  transform.py (THIS CODE RUNS IN-PROCESS, UNSANDBOXED, ON INSTALL & EVERY FRAME):")
    out("-" * 72)
    try:
        src = (pkg.dir / TRANSFORM_PY).read_text()
    except OSError as exc:                                    # pragma: no cover
        src = f"<could not read transform.py: {exc}>"
    for line in src.splitlines():
        out(f"  | {line}")
    out("-" * 72)


def run_install(source_spec: str, name: str, *,
                recipes_dir=None, trust: TrustStore | None = None,
                confirm=None, assume_reviewed: bool = False,
                out=print) -> InstallResult:
    """
    Fetch, review, and (on explicit confirmation) install recipe `name` from
    `source_spec`. Returns an InstallResult — never raises for an expected refusal
    (bad sig, duplicate, declined); `installed` is the success signal.

    `confirm` is a zero-arg callable returning the typed confirmation string (defaults
    to reading one line from stdin); `assume_reviewed` (the --yes-i-reviewed path)
    skips the prompt entirely and proceeds as the strongest confirmation. `recipes_dir`
    / `trust` are injectable for tests and non-default homes.
    """
    recipes_dir = Path(recipes_dir) if recipes_dir is not None \
        else (crypto.d2a_home() / "recipes")
    trust = trust if trust is not None else TrustStore()

    staging = Path(tempfile.mkdtemp(prefix="d2a-install-"))
    try:
        # 1. FETCH (never executes).
        try:
            src = open_source(source_spec)
            pkg_dir = src.fetch(name, staging)
        except RemoteSourceError as exc:
            return InstallResult(False, name, code=errors.RECIPE_FETCH_FAILED, reason=str(exc))

        # 2. FORMAT.
        try:
            pkg = RecipePackage.load(pkg_dir)
        except RecipeFormatError as exc:
            return InstallResult(False, name, code=errors.RECIPE_MALFORMED, reason=str(exc))

        author = pkg.author_pubkey
        version = str(pkg.recipe.get("version", ""))

        # 3. SIGNATURE (authorship) — HARD gate, unreviewable. A package whose bytes
        #    don't verify against their embedded author cannot be trust-installed.
        if not is_signed(pkg.recipe):
            return InstallResult(False, name, version, author,
                                 code=errors.RECIPE_UNSIGNED,
                                 reason="package is not signed (no sig/author_pubkey)")
        if not verify_recipe_sig(pkg.recipe):
            return InstallResult(False, name, version, author,
                                 code=errors.RECIPE_BAD_SIG,
                                 reason="signature does not verify against embedded author_pubkey")

        # 4. SCHEMA + provides manifest (a malformed recipe is refused pre-trust).
        try:
            validate_recipe_schema(pkg.recipe)
            manifest, meta = validate_provides(pkg.provides)
        except errors.DeriveError as exc:
            return InstallResult(False, name, version, author,
                                 code=exc.code, reason=exc.detail)

        # 5. DUPLICATE / UPGRADE guard.
        existing = _installed_version(recipes_dir, name)
        upgraded_from = None
        if existing is not None:
            if existing == version:
                return InstallResult(False, name, version, author,
                                     code=errors.RECIPE_DUPLICATE,
                                     reason=f"{name} v{version} is already installed "
                                            f"(identical name+version). Bump the version to "
                                            f"publish new code.")
            upgraded_from = existing            # a new version → re-review as an upgrade

        # 6. REVIEW.
        trusted = trust.is_trusted(author)
        _print_review(out, pkg, meta, manifest, trusted, upgraded_from)

        # 7. CONFIRM.
        if assume_reviewed:
            token = CONFIRM_AUTHOR_TRUST         # the escape hatch asserts full review
        else:
            prompt = (f"\nType '{CONFIRM_AUTHOR_TRUST}' to trust this NEW author and install"
                      if not trusted else
                      f"\nType '{CONFIRM_YES}' (or '{CONFIRM_AUTHOR_TRUST}') to install")
            out(prompt + "  [anything else aborts]:")
            token = (confirm() if confirm is not None else _stdin_line()).strip()

        if trusted:
            proceed = token in (CONFIRM_YES, CONFIRM_AUTHOR_TRUST)
        else:
            # a new author MUST be trusted explicitly with the stronger phrase.
            proceed = token == CONFIRM_AUTHOR_TRUST
        if not proceed:
            need = CONFIRM_AUTHOR_TRUST if not trusted else f"{CONFIRM_YES}/{CONFIRM_AUTHOR_TRUST}"
            return InstallResult(False, name, version, author,
                                 reason=f"confirmation declined (needed '{need}', got {token!r})",
                                 upgraded_from=upgraded_from or "")

        # 8. INSTALL — copy into the registry, then (if new) extend trust.
        dest = recipes_dir / name
        recipes_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(pkg_dir, dest)

        author_added = False
        if not trusted:
            trust.add(author, label=f"installed:{name}")
            author_added = True

        return InstallResult(True, name, version, author,
                             upgraded_from=upgraded_from or "", author_added=author_added)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _stdin_line() -> str:
    line = sys.stdin.readline()
    return line if line else ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m d2a_derive.install",
        description="Fetch, review, and (on typed confirmation) install a recipe package.")
    ap.add_argument("source", help="a local directory of packages OR an http(s) base URL")
    ap.add_argument("name", help="the package name to install (<source>/<name>/…)")
    ap.add_argument("--yes-i-reviewed", dest="assume_reviewed", action="store_true",
                    help="non-interactive: assert you have reviewed the transform and "
                         "trust the author; skips the typed confirmation (for scripting)")
    ap.add_argument("--recipes-dir", default=None,
                    help="registry dir to install into (default <d2a_home>/recipes)")
    args = ap.parse_args(argv)

    res = run_install(args.source, args.name,
                      recipes_dir=args.recipes_dir,
                      assume_reviewed=args.assume_reviewed)

    if res.installed:
        print(f"\ninstalled {res.name} v{res.version}")
        if res.upgraded_from:
            print(f"  upgraded from v{res.upgraded_from} (re-reviewed as new code)")
        if res.author_added:
            print(f"  trusted NEW author {fingerprint(res.author_pubkey)} "
                  f"→ added to trusted_authors.json")
        print("  run:  python -m d2a_derive.conformance " + res.name)
        return 0

    if res.code:
        print(f"\nREFUSED [{res.code}]: {res.reason}", file=sys.stderr)
    else:
        print(f"\nnot installed: {res.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
