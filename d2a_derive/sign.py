"""
d2a_derive/sign.py — the one-command recipe signing helper, with a self-check gate.

    python -m d2a_derive.sign <recipe_dir> <keyname> [--trust]

This is part of the COMMUNITY-GRADE FORMAT, not tooling sugar: a recipe author signs
their package with a single command, and the signed recipe.json is self-contained
(author_pubkey + sig embedded) so any recipient can verify authorship offline.

SELF-CHECK GATE (Phase 5). Signing is the moment a recipe becomes shippable, so it
is where the format's constitution is enforced on the AUTHOR: you cannot sign a
recipe that
  * omits its honesty fields — `provides.fidelity` must be a non-empty string and
    `provides.cannot_detect` a non-empty list (a recipe that won't state its blind
    spots does not ship), or
  * fails its own dry-run — the transform must pass its `test_frames.json`
    deterministically (you cannot sign a recipe that fails itself).
Both run BEFORE any signature is written, so an author is stopped at authoring time,
not at a reviewer's desk. (`--skip-self-check` exists only for the rare case of
re-signing a package you have already validated; it prints a loud warning.)

What it does on success:
  1. Loads (or creates) the Ed25519 keypair named <keyname> via
     crypto.load_or_create_keypair — the SAME identity primitive nodes use.
  2. Sets author_pubkey to that key and writes back a canonically-signed recipe.json
     (pretty-printed for review/diffing).
  3. Prints the author_pubkey and reminds the installer that the SIGNATURE PROVES
     AUTHORSHIP, NOT SAFETY — a recipient must still review-then-trust the author.

  --trust also adds the author's pubkey to THIS machine's trusted_authors.json
  (convenience for the author's own dev loop; a recipient never gets this for free).
"""

import argparse
import json
import sys
from pathlib import Path

from d2a import crypto
from d2a_derive import errors
from d2a_derive.dryrun import dry_run
from d2a_derive.loader import load_transform, TransformLoadError
from d2a_derive.recipe import RECIPE_JSON, RecipePackage, RecipeFormatError, sign_recipe
from d2a_derive.trust import TrustStore
from d2a_derive.validator import validate_provides


class RefuseToSign(Exception):
    """The package fails a self-check (missing honesty fields or a failing dry-run),
    so signing is refused BEFORE any signature is written."""


def check_signable(pkg: RecipePackage) -> None:
    """Enforce the format's constitution on the author. Raises RefuseToSign with a
    specific message if the recipe omits its honesty fields or fails its own dry-run.
    Loads the transform (executes author code — this is the author's own machine)."""
    provides = pkg.provides if isinstance(pkg.provides, dict) else {}

    fidelity = provides.get("fidelity")
    if not isinstance(fidelity, str) or not fidelity.strip():
        raise RefuseToSign(
            "provides.fidelity is empty — a recipe must state its honest fidelity "
            "(what the substitute can and cannot do) before it can be signed")

    cannot = provides.get("cannot_detect")
    if not isinstance(cannot, list) or not cannot or \
            not all(isinstance(c, str) and c.strip() for c in cannot):
        raise RefuseToSign(
            "provides.cannot_detect is empty — a recipe must declare at least one "
            "blind spot before it can be signed")

    # The recipe must pass its OWN frames. Validate provides → manifest, load the
    # transform, run the dry-run gate. Any failure refuses the signature.
    try:
        manifest, _meta = validate_provides(provides)
    except errors.DeriveError as exc:
        raise RefuseToSign(f"provides manifest is invalid: {exc.detail}") from exc
    try:
        module = load_transform(pkg)
    except TransformLoadError as exc:
        raise RefuseToSign(f"transform will not load: {exc}") from exc
    dr = dry_run(pkg, module, manifest)
    if not dr.ok:
        raise RefuseToSign(f"recipe fails its own dry-run — you cannot sign a recipe "
                           f"that fails itself: {dr.reason}")


def sign_recipe_dir(recipe_dir, keyname: str, trust: bool = False,
                    skip_self_check: bool = False) -> dict:
    """Sign <recipe_dir>/recipe.json in place with the keypair `keyname`, after the
    self-check gate (unless skip_self_check). Returns the signed recipe dict.
    Raises RefuseToSign if the package fails its self-check."""
    d = Path(recipe_dir)
    rj = d / RECIPE_JSON
    if not rj.is_file():
        raise FileNotFoundError(f"{rj} does not exist")

    try:
        pkg = RecipePackage.load(d)
    except RecipeFormatError as exc:
        raise RefuseToSign(f"package is malformed: {exc}") from exc

    if not skip_self_check:
        check_signable(pkg)

    keypair = crypto.load_or_create_keypair(keyname)
    signed = sign_recipe(pkg.recipe, keypair.private_key)
    rj.write_text(json.dumps(signed, indent=2, sort_keys=True) + "\n")

    if trust:
        TrustStore().add(keypair.public_key, label=f"self:{keyname}")
    return signed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m d2a_derive.sign",
        description="Ed25519-sign a recipe package's recipe.json (after a self-check).")
    ap.add_argument("recipe_dir", help="path to the recipe package directory")
    ap.add_argument("keyname", help="name of the signing keypair (persisted under d2a_home/keys)")
    ap.add_argument("--trust", action="store_true",
                    help="also add this author to THIS machine's trusted_authors.json")
    ap.add_argument("--skip-self-check", action="store_true",
                    help="re-sign without the honesty/dry-run gate (loud warning; rare)")
    args = ap.parse_args(argv)

    if args.skip_self_check:
        print("WARNING: --skip-self-check — signing WITHOUT the honesty/dry-run gate.",
              file=sys.stderr)

    try:
        signed = sign_recipe_dir(args.recipe_dir, args.keyname, trust=args.trust,
                                 skip_self_check=args.skip_self_check)
    except RefuseToSign as exc:
        print(f"REFUSED TO SIGN: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"signed {args.recipe_dir}/recipe.json")
    print(f"  recipe name : {signed.get('name')}")
    print(f"  provides    : {signed.get('provides', {}).get('name')}")
    print(f"  author_pubkey: {signed.get('author_pubkey')}")
    print()
    print("NOTE: this signature proves AUTHORSHIP, not SAFETY. A recipient must")
    print("      review-then-trust this author_pubkey (add it to trusted_authors.json)")
    print("      before the recipe will load — and loading the transform IS executing it.")
    if args.trust:
        print("  (--trust) author added to this machine's trusted_authors.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
