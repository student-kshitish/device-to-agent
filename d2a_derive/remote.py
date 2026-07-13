"""
d2a_derive/remote.py — fetch a recipe PACKAGE from a remote source.

Phase 5 turns the local recipe registry into COMMUNITY infrastructure. The first
piece is distribution: a recipe package (a directory `recipe.json` + `transform.py`
+ `test_frames.json`) can live somewhere other than `~/.d2a/recipes/` and be
fetched from there. Two source kinds, both pure stdlib:

  (a) DIRECTORY  — a local filesystem path whose layout is `<base>/<name>/…`. A git
      repository of packages that the user has ALREADY cloned satisfies this: we do
      NOT shell out to git — the user clones, we read the checkout. Any directory of
      packages works identically (a USB stick, an NFS mount, an unpacked tarball).

  (b) URL        — a raw HTTP(S) base pointing at the SAME layout, so
      `<base>/<name>/recipe.json` (etc.) are fetchable with urllib. This is what a
      raw-git host (raw.githubusercontent.com/…/recipes) or a plain static file
      server exposes; no API, no auth, no server-side code.

CRITICAL BOUNDARY: fetch() NEVER installs and NEVER executes. It copies/downloads
the three package files into a caller-supplied STAGING directory and returns the
path. Trust verification, the human review, and the copy into the registry are the
install step (install.py) — loading a transform IS executing it, so nothing here
imports transform.py. `open_source` picks the kind from the spec string.
"""

import shutil
import urllib.error
import urllib.request
from pathlib import Path

from d2a_derive.recipe import RECIPE_JSON, TRANSFORM_PY, TEST_FRAMES_JSON

# The three files that constitute a package on the wire, in fetch order (recipe.json
# first so a missing-package error is reported against the manifest, not a leaf file).
PACKAGE_FILES = (RECIPE_JSON, TRANSFORM_PY, TEST_FRAMES_JSON)

# urllib fetch timeout — a community source should answer promptly or fail; we never
# block an install indefinitely on a slow mirror.
FETCH_TIMEOUT_S = 15.0


class RemoteSourceError(Exception):
    """A remote source could not deliver a well-formed package (missing file,
    unreachable URL, unreadable path). Never a trust/verification failure — that is
    install.py's job on the fetched, staged bytes."""


class RemoteSource:
    """Base: a place recipe packages can be fetched FROM. Subclasses implement the
    per-file read; the fetch orchestration (staging dir, required files) is shared."""

    #: human label for messages
    kind = "source"

    def _read(self, name: str, filename: str) -> bytes:      # pragma: no cover - abstract
        raise NotImplementedError

    def available(self) -> list[str] | None:
        """Package names this source can enumerate, or None if it cannot list
        (a raw URL base is not browsable). Best-effort, for CLI hints only."""
        return None

    def fetch(self, name: str, staging_dir) -> Path:
        """
        Fetch package `name` into `<staging_dir>/<name>/` and return that directory.
        Fetches all three package files; a missing one is a RemoteSourceError (a
        package is not a package without its transform + frames). NEVER installs,
        verifies, or executes — the caller (install.py) does the trust review on the
        staged bytes.
        """
        if not name or "/" in name or name in (".", ".."):
            raise RemoteSourceError(f"invalid package name {name!r}")
        dest = Path(staging_dir) / name
        dest.mkdir(parents=True, exist_ok=True)
        for filename in PACKAGE_FILES:
            try:
                data = self._read(name, filename)
            except RemoteSourceError:
                raise
            except Exception as exc:                          # noqa: BLE001
                raise RemoteSourceError(
                    f"{self.kind}: could not fetch {name}/{filename}: {exc}") from exc
            (dest / filename).write_bytes(data)
        return dest


class DirectorySource(RemoteSource):
    """A local directory of packages laid out as `<base>/<name>/…`. A cloned git repo
    of recipes is exactly this — the user clones, we read the working tree."""

    kind = "directory"

    def __init__(self, base):
        self.base = Path(base)

    def _read(self, name: str, filename: str) -> bytes:
        path = self.base / name / filename
        if not path.is_file():
            raise RemoteSourceError(
                f"directory source {self.base}: {name}/{filename} not found")
        return path.read_bytes()

    def available(self) -> list[str] | None:
        if not self.base.is_dir():
            return []
        return sorted(
            child.name for child in self.base.iterdir()
            if child.is_dir() and (child / RECIPE_JSON).is_file())


class UrlSource(RemoteSource):
    """A raw HTTP(S) base serving `<base>/<name>/<file>`. Plain GETs via urllib — no
    API, no auth, no server-side code; a static file host or raw-git URL suffices."""

    kind = "url"

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _url(self, name: str, filename: str) -> str:
        return f"{self.base}/{name}/{filename}"

    def _read(self, name: str, filename: str) -> bytes:
        url = self._url(name, filename)
        try:
            with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:  # noqa: S310 (http(s) only; scheme gated in open_source)
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise RemoteSourceError(
                f"url source: {url} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RemoteSourceError(f"url source: {url} unreachable: {exc.reason}") from exc


def open_source(spec: str) -> RemoteSource:
    """
    Resolve a source SPEC string to a RemoteSource. `http://`/`https://` → UrlSource;
    anything else is treated as a local directory path (a cloned repo, a mount, an
    unpacked tarball). No other URL schemes are honoured — file://, git://, ssh://
    are intentionally NOT supported (a git URL would invite shelling out to git,
    which this module refuses to do; the user clones, we read).
    """
    s = (spec or "").strip()
    low = s.lower()
    if low.startswith(("http://", "https://")):
        return UrlSource(s)
    if "://" in low:
        raise RemoteSourceError(
            f"unsupported source scheme in {spec!r} — use an http(s) URL or a local "
            f"directory path (clone a git repo yourself and pass the checkout path)")
    return DirectorySource(s)
