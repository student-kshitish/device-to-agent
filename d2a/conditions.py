"""
d2a/conditions.py — event condition vocabulary: validation + edge evaluation.

LEAF MODULE: pure stdlib, no d2a imports. Validates a subscription condition
against a capability MANIFEST (the same fixed vocabulary manifests use) and
evaluates edge-triggered firing with re-arm. No transport / runtime imports, so
both endpoints and the device verb layer import it cycle-free.

A condition names ONE manifest reading field and one operator:

    {"field": <manifest reading field>, "op": <OP>, "value": <scalar>}

`value` is omitted for op == "changed". One condition per subscription — an
agent wanting AND/OR composes it agent-side with multiple subscriptions. There
is deliberately NO expression language; that is what keeps this spec-able.

OPS:
  gt lt ge le   ORDERED — field must be a numeric manifest field; value numeric.
  eq ne         EQUALITY — number|string|boolean field; value the same scalar type.
  changed       fires on ANY value change; no value; number|string|boolean field.

EDGE SEMANTICS (the whole point): a comparison condition fires on the SAMPLE
where its truth value transitions False→True (the crossing), NOT on every sample
while it stays True. It RE-ARMS automatically when the truth value returns to
False. The FIRST sample only establishes a baseline and NEVER fires — there is
no prior edge to cross, even if the condition is already true at the baseline.
"changed" fires whenever the value differs from the previous sample (baseline
excepted).

Conditions are validated at subscribe time against the capability's manifest:
unknown field rejected, op/type mismatch rejected (gt on a string, eq on an
array). Evaluation runs later, per sample, inside the device sampling loop.
"""

# ── vocabulary ────────────────────────────────────────────────────────────────

ORDERED_OPS = {"gt", "lt", "ge", "le"}   # require a numeric field
EQ_OPS      = {"eq", "ne"}               # require matching scalar type
OPS         = ORDERED_OPS | EQ_OPS | {"changed"}

# Fields a condition may target. Arrays / objects are NOT conditionable (you
# cannot meaningfully cross-threshold or equality-check an array) — matches the
# design ruling "eq on an array rejected".
_CONDITIONABLE_TYPES = {"number", "string", "boolean"}

_UNSET = object()   # sentinel: no baseline sample seen yet


class ConditionError(ValueError):
    """Raised when a condition violates the vocabulary or does not match the
    capability manifest. The message names the exact problem so an agent author
    can fix the subscription."""


# ── validation ────────────────────────────────────────────────────────────────

def validate_condition(condition: dict, manifest: dict) -> dict:
    """
    Validate `condition` against `manifest` (the capability's self-description).
    Returns a normalized condition dict on success; raises ConditionError
    otherwise. `value` is dropped from the result for op == "changed".

    Checks, in order: shape, unknown field, unknown op, op/field-type
    compatibility, and value presence + type.
    """
    if not isinstance(condition, dict):
        raise ConditionError("condition must be an object")

    unknown = set(condition) - {"field", "op", "value"}
    if unknown:
        raise ConditionError(f"unknown condition keys {sorted(unknown)}")

    field = condition.get("field")
    if not isinstance(field, str) or not field:
        raise ConditionError("condition 'field' is required and must be a non-empty string")

    op = condition.get("op")
    if op not in OPS:
        raise ConditionError(f"'op' must be one of {sorted(OPS)}, got {op!r}")

    reading = manifest.get("reading", {}) if isinstance(manifest, dict) else {}
    if field not in reading:
        raise ConditionError(
            f"unknown field {field!r}; manifest declares {sorted(reading)}")

    spec = reading.get(field) or {}
    ftype = spec.get("type")
    if ftype not in _CONDITIONABLE_TYPES:
        raise ConditionError(
            f"field {field!r} has type {ftype!r}; conditions require one of "
            f"{sorted(_CONDITIONABLE_TYPES)} (arrays/objects are not conditionable)")

    if op == "changed":
        # No value; a value would be meaningless — reject it so mistakes surface.
        if "value" in condition and condition["value"] is not None:
            raise ConditionError("op 'changed' takes no 'value'")
        return {"field": field, "op": op}

    if "value" not in condition:
        raise ConditionError(f"op {op!r} requires a 'value'")
    value = condition["value"]

    if op in ORDERED_OPS:
        if ftype != "number":
            raise ConditionError(
                f"op {op!r} requires a numeric field, but {field!r} is {ftype!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConditionError(f"op {op!r} requires a numeric 'value', got {value!r}")
    else:  # eq / ne — value type must match the field type
        if not _type_matches(ftype, value):
            raise ConditionError(
                f"op {op!r} on a {ftype!r} field requires a {ftype!r} 'value', got {value!r}")

    return {"field": field, "op": op, "value": value}


def _type_matches(ftype: str, value) -> bool:
    if ftype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ftype == "string":
        return isinstance(value, str)
    if ftype == "boolean":
        return isinstance(value, bool)
    return False


# ── reading field extraction ──────────────────────────────────────────────────

def extract(reading, field):
    """
    Resolve `field` to a scalar from a reading snapshot, or None if absent.

    Handles the two reading shapes this codebase produces:
      - a DataProvider frame  {"raw": {...}, "derived": {...}, "ts", "seq"}
      - a flat live_state dict {field: scalar}  (virtual capabilities)

    Resolution order: the numeric `derived` view (dotted keys), then the `raw`
    block by exact key, dotted path, or one-level source grouping (e.g. a
    virtual pseudo-source frame raw == {"smart_sensor": {"value": ...}}), then
    the reading itself when it is already flat. None means "not present this
    sample" — the evaluator treats that as the condition being false.
    """
    if not isinstance(reading, dict):
        return None

    derived = reading.get("derived")
    if isinstance(derived, dict):
        d = derived.get(field)
        if isinstance(d, dict) and "value" in d:
            return d["value"]

    raw = reading.get("raw") if "raw" in reading else reading
    if isinstance(raw, dict):
        if field in raw and not isinstance(raw[field], dict):
            return raw[field]
        if "." in field:
            cur = raw
            for part in field.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = _UNSET
                    break
            if cur is not _UNSET and not isinstance(cur, dict):
                return cur
        for v in raw.values():
            if isinstance(v, dict) and field in v and not isinstance(v[field], dict):
                return v[field]

    return None


# ── edge evaluation ───────────────────────────────────────────────────────────

def _truth(op: str, current, value) -> bool:
    """Truth value of a comparison op for one sample. An absent (None) reading is
    never true — a threshold cannot be crossed by a value that is not present."""
    if current is None:
        return False
    try:
        if op == "gt": return current >  value
        if op == "lt": return current <  value
        if op == "ge": return current >= value
        if op == "le": return current <= value
        if op == "eq": return current == value
        if op == "ne": return current != value
    except TypeError:
        return False
    return False


class EdgeEvaluator:
    """
    Per-subscription edge/re-arm state machine for ONE validated condition.

    Feed it one reading per sample via update(); it returns True on exactly the
    samples that should deliver an event. State (baseline seen, previous truth,
    previous value) is private to this evaluator, so N event subscriptions on the
    same capability each track their own edges off the one shared frame.
    """

    def __init__(self, condition: dict):
        self.field = condition["field"]
        self.op    = condition["op"]
        self.value = condition.get("value")
        self._prev_true = False
        self._last      = _UNSET
        self._seen      = False

    def update(self, reading) -> bool:
        """Feed one sample. Return True iff this sample is a firing edge."""
        current = extract(reading, self.field)

        if self.op == "changed":
            if not self._seen:                 # baseline — never fires
                self._seen = True
                self._last = current
                return False
            fired = (current != self._last)
            self._last = current
            return fired

        now_true = _truth(self.op, current, self.value)
        if not self._seen:                     # baseline — never fires, even if true
            self._seen = True
            self._prev_true = now_true
            return False
        fired = now_true and not self._prev_true   # False→True crossing only
        self._prev_true = now_true                 # True→False here re-arms
        return fired
