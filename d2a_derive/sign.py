"""
d2a_derive/sign.py — the one-command recipe signing helper.

    python -m d2a_derive.sign <recipe_dir> <keyname> [--trust]

This is part of the COMMUNITY-GRADE FORMAT, not tooling sugar: a future recipe
author signs their package with a single command, and the signed recipe.json is
self-contained (author_pubkey + sig embedded) so any recipient can verify
authorship offline.

What it does:
  1. Loads (or creates) the Ed25519 keypair named <keyname> via
     crypto.load_or_create_keypair — the SAME identity primitive nodes use, so an
     author signs recipes with a stable, persisted key.
  2. Reads <recipe_dir>/recipe.json, sets author_pubkey to that key, and writes
     back a canonically-signed recipe.json (pretty-printed for review/diffing).
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
from d2a_derive.recipe import RECIPE_JSON, sign_recipe
from d2a_derive.trust import TrustStore


def sign_recipe_dir(recipe_dir, keyname: str, trust: bool = False) -> dict:
    """Sign <recipe_dir>/recipe.json in place with the keypair `keyname`. Returns
    the signed recipe dict."""
    d = Path(recipe_dir)
    rj = d / RECIPE_JSON
    if not rj.is_file():
        raise FileNotFoundError(f"{rj} does not exist")

    recipe = json.loads(rj.read_text())
    keypair = crypto.load_or_create_keypair(keyname)
    signed = sign_recipe(recipe, keypair.private_key)
    rj.write_text(json.dumps(signed, indent=2, sort_keys=True) + "\n")

    if trust:
        TrustStore().add(keypair.public_key, label=f"self:{keyname}")
    return signed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m d2a_derive.sign",
        description="Ed25519-sign a recipe package's recipe.json.")
    ap.add_argument("recipe_dir", help="path to the recipe package directory")
    ap.add_argument("keyname", help="name of the signing keypair (persisted under d2a_home/keys)")
    ap.add_argument("--trust", action="store_true",
                    help="also add this author to THIS machine's trusted_authors.json")
    args = ap.parse_args(argv)

    try:
        signed = sign_recipe_dir(args.recipe_dir, args.keyname, trust=args.trust)
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
