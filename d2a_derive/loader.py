"""
d2a_derive/loader.py — load a recipe's transform.py via importlib.

HONESTY STATEMENT (the whole reason this is its own small module):

    LOADING transform.py IS EXECUTING it. importlib.exec_module runs the file's
    module-level code, and every subsequent init()/on_frame()/reading() call runs
    recipe-author code IN-PROCESS and UNSANDBOXED. There is no AST filter, no
    seccomp, no subprocess isolation.

    Trust v1 therefore verifies AUTHORSHIP, not SAFETY. The only structural
    safeguard is ORDERING: registry.py runs the trust gate (signature verifies
    against the embedded author_pubkey AND that key is in trusted_authors.json)
    STRICTLY BEFORE calling this loader, so untrusted code is never imported. A
    trusted author's bug or malice is out of scope for v1 and is documented as a
    known limitation, not silently mitigated.

Deterministic + stdlib-only is a CONVENTION the reference recipes follow and the
dry-run enforces (determinism), not something importlib can guarantee. A recipe
MAY declare "deps": [...]; a declared dep that will not import simply fails the
match (handled in registry.py), rather than crashing at transform time.
"""

import importlib.util
import uuid

from d2a_derive.recipe import RecipePackage

# The transform contract: these three callables must exist on the module.
_REQUIRED_CALLABLES = ("init", "on_frame", "reading")


class TransformLoadError(Exception):
    """transform.py could not be imported or is missing a required callable.
    (Raised only AFTER the trust gate has passed — see module docstring.)"""


def load_transform(pkg: RecipePackage):
    """
    Import <pkg.dir>/transform.py and return the module. Assumes the caller
    (registry.py) has ALREADY passed the trust gate — importing runs author code.
    Raises TransformLoadError if import fails or the transform contract
    (init / on_frame / reading) is not satisfied.
    """
    path = pkg.transform_path
    # Unique module name per load so re-loading a recipe never collides in
    # sys.modules with a previously loaded version.
    mod_name = f"d2a_derive._recipe_{pkg.name or 'anon'}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise TransformLoadError(f"{path}: could not create import spec")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)          # <-- executes author code
    except Exception as exc:                     # noqa: BLE001 — surface any author error
        raise TransformLoadError(f"{path}: transform failed to import: {exc}") from exc

    for fn in _REQUIRED_CALLABLES:
        if not callable(getattr(module, fn, None)):
            raise TransformLoadError(f"{path}: transform missing required callable '{fn}(...)'")
    return module
