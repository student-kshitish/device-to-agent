"""
tests/test_errors.py — the drift guard for the unified error model (v1.4).

Two kinds of enforcement:

  1. Registry integrity — d2a/errors.py has no duplicate code VALUES, and its
     ALL_CODES set is exactly the module's string constants. A code added without
     registering it (or a copy-pasted duplicate value) fails here.

  2. Source scan — the wire-facing modules may not emit a SIXTH error shape. Every
     protocol error/denial dict literal must carry its code under the single `code`
     key, that code (when a string literal) must be a registry member, and the
     abolished carriers `reason`/`error` may not reappear as the code key. If a new
     shape sneaks in, this test fails.

BOUNDARY: codes that live INSIDE action_result.result (Guardian/emergent brain
results — Tier F) are application-level, NOT registry members, and are explicitly
out of scope for the scan (they are not protocol error/denial dicts: no
type=="error", no status in {"denied","error"}).
"""

import ast
import os
import unittest

from d2a import errors

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The wire-facing surface: everything that constructs an error/denial that crosses
# (or is parsed off) the transport. Guardian/emergent modules are deliberately
# excluded — they own the Tier-F application-level namespace.
WIRE_MODULES = [
    "d2a/swarm.py",
    "d2a/broker.py",
    "runtimes/device_runtime.py",
    "agents/remote_agent.py",
    "agents/simple_agent.py",
]

# A dict literal is a "protocol error/denial" if it declares (with a constant
# value) type=="error" or status in {"denied","error"}.
_DENIAL_STATUSES = {"denied", "error"}


class TestRegistryIntegrity(unittest.TestCase):

    def _named_codes(self):
        return {n: v for n, v in vars(errors).items()
                if isinstance(v, str) and not n.startswith("_")}

    def test_no_duplicate_code_values(self):
        codes = self._named_codes()
        values = list(codes.values())
        dupes = {v for v in values if values.count(v) > 1}
        self.assertFalse(dupes, f"duplicate code values in errors.py: {dupes}")

    def test_all_codes_matches_constants(self):
        # ALL_CODES must equal exactly the set of string-constant values — so a new
        # constant that is not added to ALL_CODES (or vice versa) fails immediately.
        self.assertEqual(set(self._named_codes().values()), set(errors.ALL_CODES))

    def test_error_helper_shape(self):
        m = errors.error(errors.INVALID_CONDITION, "bad field", binding_id="b1")
        self.assertEqual(m["type"], "error")
        self.assertEqual(m["code"], errors.INVALID_CONDITION)
        self.assertEqual(m["detail"], "bad field")
        self.assertEqual(m["binding_id"], "b1")

    def test_wire_error_carries_code(self):
        e = errors.WireError.from_response(
            {"type": "error", "code": errors.NO_RESPONSE, "detail": "x", "binding_id": "b"})
        self.assertEqual(e.code, errors.NO_RESPONSE)
        self.assertEqual(e.binding_id, "b")


def _const_keys(dict_node):
    """Map constant string keys → their value nodes for one ast.Dict."""
    out = {}
    for k, v in zip(dict_node.keys, dict_node.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out[k.value] = v
    return out


def _is_protocol_error_dict(keys):
    t = keys.get("type")
    s = keys.get("status")
    if isinstance(t, ast.Constant) and t.value == "error":
        return True
    if isinstance(s, ast.Constant) and s.value in _DENIAL_STATUSES:
        return True
    return False


class TestWireSourceScan(unittest.TestCase):
    """Static AST scan — no imports of the wire modules needed."""

    def _dicts(self, relpath):
        with open(os.path.join(_REPO, relpath), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=relpath)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                yield node

    def test_literal_codes_are_registered(self):
        # Anywhere a dict literal writes "code": "<string literal>", that literal
        # must be a registry member. (errors.CONSTANT references are Names, checked
        # by registry integrity above; this catches a raw literal drifting in.)
        for mod in WIRE_MODULES:
            for d in self._dicts(mod):
                keys = _const_keys(d)
                cv = keys.get("code")
                if isinstance(cv, ast.Constant) and isinstance(cv.value, str):
                    self.assertIn(cv.value, errors.ALL_CODES,
                                  f"{mod}: unregistered code literal {cv.value!r}")

    def test_no_sixth_shape(self):
        # Every protocol error/denial dict literal must carry `code`, and must NOT
        # resurrect the abolished carriers `reason`/`error` as the code field.
        for mod in WIRE_MODULES:
            for d in self._dicts(mod):
                keys = _const_keys(d)
                if not _is_protocol_error_dict(keys):
                    continue
                self.assertNotIn("reason", keys,
                                 f"{mod}: abolished carrier 'reason' in an error/denial dict")
                self.assertNotIn("error", keys,
                                 f"{mod}: abolished carrier 'error' in an error/denial dict "
                                 f"(free-text task failures use 'error_detail')")
                self.assertIn("code", keys,
                              f"{mod}: error/denial dict without a registry 'code'")


if __name__ == "__main__":
    unittest.main()
