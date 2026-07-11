"""
d2a_derive/recipe.py — the RECIPE PACKAGE on-disk format + canonical signing.

A recipe package is a directory:

    <name>/
        recipe.json        # signed manifest of the recipe (this module owns it)
        transform.py       # deterministic, stdlib-only Python (loader.py loads it)
        test_frames.json   # dry-run fixtures (dryrun.py replays them)

SIGNING (reuses d2a.crypto primitives verbatim — no new crypto):
    The signature is Ed25519 over canonical_json(recipe MINUS "sig"). The signer's
    key is the recipe's own `author_pubkey` field, which stays INSIDE the signed
    bytes so it cannot be swapped without invalidating the signature; the "sig"
    hex is the OUTPUT, added outside the signed bytes. This mirrors
    d2a.signing.verify_record's hand-rolled canonical pattern rather than
    crypto.sign_dict (which would impose its own "sig_key" field name).

This module does NOT validate the recipe's schema or load its transform — that is
validator.py / loader.py, gated by trust in registry.py.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from d2a import crypto

RECIPE_JSON = "recipe.json"
TRANSFORM_PY = "transform.py"
TEST_FRAMES_JSON = "test_frames.json"


class RecipeFormatError(Exception):
    """The package directory is missing files or recipe.json is unparseable."""


# ── canonical signing (crypto primitives reused; author_pubkey is the signer) ──

def recipe_signing_bytes(recipe: dict) -> bytes:
    """Canonical bytes that the signature covers: the recipe minus its "sig"
    field (author_pubkey stays in — it is signed)."""
    payload = {k: v for k, v in recipe.items() if k != "sig"}
    return crypto.canonical_json(payload)


def sign_recipe(recipe: dict, private_hex: str) -> dict:
    """
    Return a signed copy of `recipe`. Sets author_pubkey to the key derived from
    private_hex (so the embedded author always matches the signer), then attaches
    a detached Ed25519 signature over the canonical bytes. Any pre-existing "sig"
    is dropped before signing.
    """
    author_pub = crypto.public_from_private(private_hex)
    out = {k: v for k, v in recipe.items() if k != "sig"}
    out["author_pubkey"] = author_pub
    sig = crypto.sign(recipe_signing_bytes(out), private_hex)
    return {**out, "sig": sig.hex()}


def verify_recipe_sig(recipe: dict) -> bool:
    """
    True iff `recipe` carries a "sig" + "author_pubkey" and the signature verifies
    against that embedded pubkey. Never raises. This proves AUTHORSHIP only — it
    says who signed these bytes, nothing about what the transform does.
    """
    if not isinstance(recipe, dict) or "sig" not in recipe or "author_pubkey" not in recipe:
        return False
    try:
        sig = bytes.fromhex(recipe["sig"])
    except (ValueError, TypeError):
        return False
    return crypto.verify(recipe_signing_bytes(recipe), sig, recipe.get("author_pubkey", ""))


def is_signed(recipe: dict) -> bool:
    """True iff both signature fields are present (does not check validity)."""
    return isinstance(recipe, dict) and bool(recipe.get("sig")) and bool(recipe.get("author_pubkey"))


# ── on-disk package ───────────────────────────────────────────────────────────

@dataclass
class RecipePackage:
    """A recipe package loaded off disk — RAW: parsed but neither verified,
    validated, nor executed. registry.py turns this into a LoadedRecipe only after
    the trust gate + validation + dry-run admission."""
    dir: Path
    recipe: dict

    @classmethod
    def load(cls, directory) -> "RecipePackage":
        """Read <dir>/recipe.json and confirm transform.py + test_frames.json exist.
        Raises RecipeFormatError if the package is structurally malformed."""
        d = Path(directory)
        rj = d / RECIPE_JSON
        if not rj.is_file():
            raise RecipeFormatError(f"{d}: missing {RECIPE_JSON}")
        if not (d / TRANSFORM_PY).is_file():
            raise RecipeFormatError(f"{d}: missing {TRANSFORM_PY}")
        if not (d / TEST_FRAMES_JSON).is_file():
            raise RecipeFormatError(f"{d}: missing {TEST_FRAMES_JSON}")
        try:
            recipe = json.loads(rj.read_text())
        except (ValueError, OSError) as exc:
            raise RecipeFormatError(f"{d}: unparseable {RECIPE_JSON}: {exc}") from exc
        if not isinstance(recipe, dict):
            raise RecipeFormatError(f"{d}: {RECIPE_JSON} is not a JSON object")
        return cls(dir=d, recipe=recipe)

    def load_test_frames(self) -> list:
        """Parse test_frames.json (a list of normalized input frames)."""
        frames = json.loads((self.dir / TEST_FRAMES_JSON).read_text())
        if not isinstance(frames, list):
            raise RecipeFormatError(f"{self.dir}: {TEST_FRAMES_JSON} must be a JSON list")
        return frames

    @property
    def transform_path(self) -> Path:
        return self.dir / TRANSFORM_PY

    # convenience accessors (raw — may be absent/ill-typed until validated)
    @property
    def name(self) -> str:
        return self.recipe.get("name", "")

    @property
    def author_pubkey(self) -> str:
        return self.recipe.get("author_pubkey", "")

    @property
    def provides(self) -> dict:
        return self.recipe.get("provides", {})

    @property
    def requires(self) -> list:
        return self.recipe.get("requires", [])

    @property
    def unit_adaptations(self) -> dict:
        return self.recipe.get("unit_adaptations", {})

    @property
    def cost_rank_hint(self) -> int:
        return self.recipe.get("cost_rank_hint", 0)

    @property
    def deps(self) -> list:
        return self.recipe.get("deps", [])
