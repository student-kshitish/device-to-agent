"""
d2a/sense/feature_extractor.py — FeatureExtractor: normalized signals → ML vector.

Maintains a small rolling buffer per numeric field across successive reads so it
can compute deltas (change since last read) and rates (change per second).

Output:
  "vector"               : flat list[float] — triplet (value, delta, rate) per field
  "names"                : list[str] aligned to vector
  "suggested_processing" : "heavy" if data is complete; "light" if many fields absent

Delta and rate are 0.0 on the first read (no previous sample to diff against).
Thread-safe: on-demand calls and streaming loops may call extract() concurrently.
"""

import threading
import time
from collections import deque

# Rolling buffer depth per field.
# 20 readings at 1–5 Hz = 4–20 seconds of history for trend detection.
FEATURE_BUFFER_DEPTH: int = 20

# If more than this fraction of registered sources are unavailable, suggest "light"
# processing so ML agents know they're working with degraded sensory input.
COMPLETENESS_LIGHT_THRESHOLD: float = 0.50  # >50% sources unavailable → "light"


def _flatten_numeric(d: dict, prefix: str = "") -> dict[str, float]:
    """Recursively flatten nested dict to dot-separated numeric-only keys.
    Skips keys starting with '_' (internal sentinels like _unavailable).
    Skips lists (e.g. temps_c, zones) — only scalars and nested dicts are walked.
    """
    result: dict = {}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten_numeric(v, key))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            result[key] = float(v)
    return result


class FeatureExtractor:
    """
    Ingests successive normalized dicts and emits ML-ready feature vectors.

    For each numeric field extracted from the normalized dict, the vector contains
    a triple: (current_value, delta_vs_prev, rate_per_sec). Names are aligned.
    The same field key always appears at the same position in successive calls
    provided the source set doesn't change between reads.
    """

    def __init__(self) -> None:
        self._buffers: dict[str, deque] = {}  # field_key → deque[(ts, value)]
        self._lock = threading.Lock()

    def _get_buf(self, key: str) -> deque:
        # caller must hold self._lock
        if key not in self._buffers:
            self._buffers[key] = deque(maxlen=FEATURE_BUFFER_DEPTH)
        return self._buffers[key]

    def extract(self, normalized: dict) -> dict:
        """
        Ingest the normalized dict, update rolling buffers, and return:
          "vector"               : flat list[float]
          "names"                : list[str] aligned to vector
          "suggested_processing" : "heavy" or "light"
        """
        now = time.time()
        flat = _flatten_numeric(normalized)

        vector: list[float] = []
        names:  list[str]   = []

        with self._lock:
            for field_key in sorted(flat.keys()):
                value = flat[field_key]
                buf   = self._get_buf(field_key)
                buf.append((now, value))

                delta = 0.0
                rate  = 0.0
                if len(buf) >= 2:
                    prev_ts, prev_val = buf[-2]
                    dt    = now - prev_ts
                    delta = value - prev_val
                    rate  = delta / dt if dt > 0 else 0.0

                vector.extend([value, round(delta, 6), round(rate, 6)])
                names.extend([
                    field_key,
                    f"{field_key}.delta",
                    f"{field_key}.rate_per_sec",
                ])

        # Decide processing hint based on source availability.
        total_sources     = len(normalized)
        unavailable_count = sum(
            1 for v in normalized.values()
            if isinstance(v, dict) and v.get("_unavailable")
        )
        if total_sources > 0:
            unavail_frac = unavailable_count / total_sources
        else:
            unavail_frac = 1.0

        suggested = "light" if unavail_frac > COMPLETENESS_LIGHT_THRESHOLD else "heavy"

        return {
            "vector":               vector,
            "names":                names,
            "suggested_processing": suggested,
        }
