"""
d2a_derive/trust.py — the user's explicit recipe-author trust store.

TRUST v1 is deliberately minimal and HONEST about its scope:

  A recipe loads ONLY if BOTH hold:
    (a) its signature verifies against its embedded author_pubkey  (recipe.py), and
    (b) that author_pubkey is present in this store  (trusted_authors.json).

Adding an author here is the "install" step: a human reviews a recipe and its
author, then adds the key. This proves you CHOSE to trust who wrote the recipe.
It does NOT prove the recipe is safe — see the loader/README honesty statement:
loading the transform IS executing it, in-process and unsandboxed.

Persisted to <d2a_home>/trusted_authors.json as {pubkey_hex: label}. A bare JSON
list of pubkey hex strings is also accepted on read (backward/forward tolerant).
"""

import json
from pathlib import Path

from d2a import crypto


class TrustStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else (crypto.d2a_home() / "trusted_authors.json")
        self._authors: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (ValueError, OSError):
            self._authors = {}
            return
        if isinstance(data, dict):
            self._authors = {str(k): str(v) for k, v in data.items()}
        elif isinstance(data, list):                     # tolerate a bare pubkey list
            self._authors = {str(k): "" for k in data}
        else:
            self._authors = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._authors, indent=2, sort_keys=True))

    def is_trusted(self, author_pubkey: str) -> bool:
        return bool(author_pubkey) and author_pubkey in self._authors

    def add(self, author_pubkey: str, label: str = "") -> None:
        """Explicitly trust an author's key (the review-then-trust install step)."""
        self._authors[author_pubkey] = label
        self._save()

    def remove(self, author_pubkey: str) -> None:
        if author_pubkey in self._authors:
            del self._authors[author_pubkey]
            self._save()

    def authors(self) -> dict[str, str]:
        return dict(self._authors)
