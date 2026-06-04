"""
d2a/preprocessor.py — structures a single fresh kernel reading into an agent-ready frame.

Runs ON the device, locally. No background loop — ring buffers updated each time
a reading is ingested (on-demand or via streaming loop).
"""

import statistics
import threading
import time
from collections import deque


class Preprocessor:
    """
    Converts raw kernel numbers into structured frames with delta/rate/rolling stats.

    Ring buffer (deque, max WINDOW entries) per numeric field.
    Thread-safe: on-demand reads and streaming loop may call make_frame concurrently.
    """

    WINDOW = 20  # ring buffer depth per field

    def __init__(self) -> None:
        self._buffers: dict[str, deque] = {}  # field_key -> deque[(ts, value)]
        self._seq = 0
        self._lock = threading.Lock()

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _flatten(d: dict, prefix: str = "") -> dict[str, float]:
        """Recursively flatten nested dict to dot-separated numeric keys only."""
        result: dict = {}
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                result.update(Preprocessor._flatten(v, key))
            elif isinstance(v, (int, float)):
                result[key] = float(v)
        return result

    def _get_buf(self, key: str) -> deque:
        # caller must hold self._lock
        if key not in self._buffers:
            self._buffers[key] = deque(maxlen=self.WINDOW)
        return self._buffers[key]

    # ── public API ─────────────────────────────────────────────────────────────

    def ingest(self, raw: dict) -> None:
        """Store a timestamped reading into ring buffers (without building a frame)."""
        ts   = time.time()
        flat = self._flatten(raw)
        with self._lock:
            for key, value in flat.items():
                self._get_buf(key).append((ts, value))

    def make_frame(self, raw: dict) -> dict:
        """
        Ingest the fresh reading and return a structured frame:
          "raw"     : the original reading dict
          "derived" : per numeric field {value, delta, rate_per_sec, avg_window, min, max}
                      delta/rate vs previous ingested reading; 0 on first read.
          "ts"      : time.time()
          "seq"     : monotonically incrementing frame counter
        """
        ts   = time.time()
        flat = self._flatten(raw)

        with self._lock:
            # ingest
            for key, value in flat.items():
                self._get_buf(key).append((ts, value))

            self._seq += 1
            seq = self._seq

            # compute derived stats from ring buffers
            derived: dict = {}
            for key, value in flat.items():
                buf  = self._get_buf(key)
                vals = [v for _, v in buf]

                delta = 0.0
                rate  = 0.0
                if len(buf) >= 2:
                    prev_ts,  prev_val  = buf[-2]
                    curr_ts,  curr_val  = buf[-1]
                    delta = curr_val - prev_val
                    dt    = curr_ts  - prev_ts
                    rate  = delta / dt if dt > 0 else 0.0

                derived[key] = {
                    "value":       value,
                    "delta":       round(delta, 4),
                    "rate_per_sec": round(rate, 4),
                    "avg_window":  round(statistics.mean(vals), 4) if vals else value,
                    "min":         min(vals),
                    "max":         max(vals),
                }

        return {
            "raw":     raw,
            "derived": derived,
            "ts":      time.time(),
            "seq":     seq,
        }
