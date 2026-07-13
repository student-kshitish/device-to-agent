"""
d2a_derive/errors.py — derivation-engine refusal codes.

APPLICATION-LEVEL, by design. These codes are NOT members of d2a.errors.ALL_CODES
and never cross the transport: derivation adds no wire verbs, so a derivation
refusal is a LOCAL, agent-side decision (a recipe wouldn't load; a plan couldn't
be built). This is exactly the Tier-F boundary the unified error model already
draws for results that ride nested inside a successful response — see
d2a/errors.py's BOUNDARY note. The wire-error drift-guard (tests/test_errors.py)
scans only the wire modules and is unaffected by anything here.

Two families:

  REGISTRY-LOAD refusals (a recipe is rejected before it can ever be planned):
      RECIPE_UNSIGNED, RECIPE_BAD_SIG, RECIPE_UNTRUSTED_AUTHOR, RECIPE_INVALID,
      RECIPE_MALFORMED, DRYRUN_FAILED

  PLAN-TIME refusals (a need cannot be satisfied by derivation right now):
      NO_RECIPE, CONTRACT_UNSATISFIED

DRYRUN_FAILED spans both: it is the admission gate at load time, and the planner
re-affirms it before emitting a plan.
"""

# ── registry-load refusals ───────────────────────────────────────────────────
RECIPE_MALFORMED       = "recipe_malformed"          # dir/files missing or unparseable JSON
RECIPE_UNSIGNED        = "recipe_unsigned"           # no sig / author_pubkey present
RECIPE_BAD_SIG         = "recipe_bad_signature"      # sig present but does not verify
RECIPE_UNTRUSTED_AUTHOR = "recipe_untrusted_author"  # valid sig, author not in trusted_authors.json
RECIPE_INVALID         = "recipe_invalid"            # schema / provides-manifest vocabulary failure
DRYRUN_FAILED          = "dryrun_failed"             # transform failed / non-deterministic on its own frames

# ── distribution refusals (Phase 5: remote source / install) ─────────────────
RECIPE_FETCH_FAILED    = "recipe_fetch_failed"       # remote source could not deliver the package
RECIPE_DUPLICATE       = "recipe_duplicate"          # name+version already installed (a new version is new code)

# ── quality-gate refusal (Phase 6: observed-cost / quarantine) ────────────────
RECIPE_QUARANTINED     = "recipe_quarantined"        # recipe flagged by failure rate / conformance; needs explicit opt-in

# ── plan-time refusals ───────────────────────────────────────────────────────
NO_RECIPE            = "no_recipe"                    # no recipe provides the needed capability
CONTRACT_UNSATISFIED = "contract_unsatisfied"        # recipe(s) matched by name, but no provider satisfies requires
# ── plan-time refusals specific to MULTI-HOP CHAINING (Phase 4) ───────────────
DERIVATION_CYCLE     = "derivation_cycle"             # a recipe transitively requires its own provides
DEPTH_EXCEEDED       = "derivation_depth_exceeded"    # satisfying the need would exceed MAX_DERIVATION_DEPTH

ALL_DERIVE_CODES = frozenset({
    RECIPE_MALFORMED, RECIPE_UNSIGNED, RECIPE_BAD_SIG, RECIPE_UNTRUSTED_AUTHOR,
    RECIPE_INVALID, DRYRUN_FAILED, RECIPE_FETCH_FAILED, RECIPE_DUPLICATE,
    RECIPE_QUARANTINED,
    NO_RECIPE, CONTRACT_UNSATISFIED, DERIVATION_CYCLE, DEPTH_EXCEEDED,
})


class DeriveError(Exception):
    """
    Raised for a derivation refusal. Carries a stable `.code` from this module
    (never a wire code) plus a human `.detail`, so callers branch on the code and
    the distinct load-time refusals (bad sig vs untrusted author vs invalid
    vocabulary) stay tellable apart.
    """
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}" + (f": {detail}" if detail else ""))
