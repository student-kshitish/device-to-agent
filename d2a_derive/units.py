"""
d2a_derive/units.py — the TINY declared-pair unit scale table.

Deliberately NOT a general unit-conversion system. The contract validator only
ever needs to (a) confirm a recipe's DECLARED unit adaptation is one the engine
can actually apply, and (b) scale an input value when it does. So this table
holds ONLY multiplicative (offset-free) conversions between compatible units.

An adaptation a recipe declares but this table does not know FAILS THE MATCH —
the same "declared-but-unsupported → refuse" discipline as the [crypto] extra.
Offset conversions (e.g. °F↔°C) are intentionally absent: they are not pure
scales, and "adapter synthesis beyond declared units" is out of scope for v1.
"""

# (from_unit, to_unit) -> multiplicative factor.  value_to = value_from * factor
_SCALE: dict[tuple[str, str], float] = {
    ("cm", "m"):  0.01,
    ("mm", "m"):  0.001,
    ("m",  "cm"): 100.0,
    ("m",  "mm"): 1000.0,
    ("km", "m"):  1000.0,
    ("m",  "km"): 0.001,
    ("g",  "kg"): 0.001,
    ("kg", "g"):  1000.0,
    ("ms", "s"):  0.001,
    ("s",  "ms"): 1000.0,
}


def can_adapt(from_unit: str, to_unit: str) -> bool:
    """True iff the engine can convert from_unit -> to_unit (identity or a known scale)."""
    return from_unit == to_unit or (from_unit, to_unit) in _SCALE


def scale_factor(from_unit: str, to_unit: str) -> float | None:
    """Multiplicative factor for from_unit -> to_unit, or None if unsupported.
    Identity (same unit) returns 1.0."""
    if from_unit == to_unit:
        return 1.0
    return _SCALE.get((from_unit, to_unit))
