"""
d2a/sense_layer.py — SenseLayer orchestrator (Part 1 of 2).

CONCEPT: Bare hardware produces raw device-specific signals; agents want clean
intent-level data. The Sense Layer translates raw → the shape the agent asked
for, and translates agent intent → the right reads.

This module (Part 1) wires the full forward pipeline:
  IntentMatcher → RawCollector → Normalizer → FeatureExtractor → VerdictEngine
  with a ConfidenceEngine spanning all stages.

Part 2 will add:
  - SafetyFilter  (pre-return veto, fills SenseFrame.vetoed / veto_reason)
  - ReflexPath    (urgent fast-path skipping optional stages)
  - EventEmitter  (publish verdict-change events to subscribers)
  - HealthAggregator (rolling device health history)

Each Part 2 hook is marked with a TODO comment in handle() so the insertion
points are explicit and zero code needs to move.
"""

import threading
import time
from collections import defaultdict

from d2a.sense_types import SenseRequest, SenseFrame, VALID_SHAPES
from d2a.sense.intent_matcher    import IntentMatcher
from d2a.sense.raw_collector     import RawCollector
from d2a.sense.normalizer        import Normalizer
from d2a.sense.feature_extractor import FeatureExtractor
from d2a.sense.verdict_engine    import VerdictEngine
from d2a.sense.confidence_engine import ConfidenceEngine


class SenseLayer:
    """
    One SenseLayer instance per DeviceRuntime.
    Nothing runs until handle() is called.

    handle() always returns verdict + advice + confidence regardless of the
    requested shape so even a caller requesting "raw" gets the health judgment.
    """

    def __init__(self, sources_by_capability: dict, device_class: str) -> None:
        self.device_class       = device_class
        self._intent_matcher    = IntentMatcher(sources_by_capability)
        self._raw_collector     = RawCollector()
        self._normalizer        = Normalizer()
        self._feature_extractor = FeatureExtractor()
        self._verdict_engine    = VerdictEngine()
        self._confidence_engine = ConfidenceEngine()

        self._seqs: dict[str, int] = defaultdict(int)   # per-resource sequence counters
        self._last_verdicts: dict[str, str | None] = {}  # for Part 2 event hook
        self._lock = threading.Lock()

    def handle(self, request: SenseRequest) -> SenseFrame:
        """
        Run the full forward pipeline for one SenseRequest.
        Returns a SenseFrame whose .data matches request.shape.
        """
        ts = time.time()

        # ── TODO (Part 2): urgent/reflex fast-path ────────────────────────────
        # Skip optional pipeline stages when speed is critical:
        # if request.mode == "urgent":
        #     return self._reflex_path(request, ts)

        # ── Step 1: resolve intent → sources ─────────────────────────────────
        sources = self._intent_matcher.resolve(request)
        if not sources:
            with self._lock:
                self._seqs[request.resource] += 1
                seq = self._seqs[request.resource]
            return SenseFrame(
                resource=request.resource,
                shape=request.shape,
                data=None,
                verdict=None,
                advice=None,
                confidence=0.0,
                device_class=self.device_class,
                ts=ts,
                seq=seq,
            )

        # ── Step 2: collect raw readings (fresh, right now) ───────────────────
        raw = self._raw_collector.collect(sources)

        # ── Step 3: normalize raw signals to 0..1 ────────────────────────────
        normalized = self._normalizer.normalize(raw)

        # ── Step 4: extract features (delta, rate, vector) ───────────────────
        features = self._feature_extractor.extract(normalized)

        # ── Step 5: judge device health ───────────────────────────────────────
        verdict, advice = self._verdict_engine.judge(normalized, features)

        # ── Step 6: compute sensory confidence ───────────────────────────────
        confidence = self._confidence_engine.score(raw, normalized)

        # ── Step 7: choose data payload by requested shape ───────────────────
        shape = request.shape if request.shape in VALID_SHAPES else "normalized"
        if shape == "raw":
            data = raw
        elif shape == "normalized":
            data = normalized
        elif shape == "features":
            data = features
        else:  # "verdict"
            data = {"verdict": verdict, "advice": advice}

        # ── Step 8: increment per-resource sequence counter ───────────────────
        with self._lock:
            self._seqs[request.resource] += 1
            seq = self._seqs[request.resource]
            prev_verdict = self._last_verdicts.get(request.resource)
            self._last_verdicts[request.resource] = verdict

        # ── TODO (Part 2): emit event on verdict change ───────────────────────
        # if verdict != prev_verdict and prev_verdict is not None:
        #     self._event_emitter.emit("verdict_change", {
        #         "resource": request.resource,
        #         "from": prev_verdict,
        #         "to": verdict,
        #         "ts": ts,
        #     })

        frame = SenseFrame(
            resource=request.resource,
            shape=shape,
            data=data,
            verdict=verdict,
            advice=advice,
            confidence=confidence,
            device_class=self.device_class,
            ts=ts,
            seq=seq,
        )

        # ── TODO (Part 2): safety_check() veto before return ──────────────────
        # frame = self._safety_filter.check(frame)

        return frame
