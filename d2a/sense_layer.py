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

    def __init__(self, sources_by_capability: dict, device_class: str,
                 event_emitter=None) -> None:
        self.device_class       = device_class
        # Part 2 (v1.3) event hook. A callable(event_type: str, payload: dict)
        # invoked on a verdict TRANSITION. None = no sink wired (default).
        self._event_emitter     = event_emitter
        # Part 2 (v1.3) safety_check hook. A callable(frame) -> frame, run as the
        # final pre-return step: it may veto (set frame.vetoed/veto_reason) OR
        # drive a device-LOCAL reflex (condition → local action, no agent). None
        # = no hook (default). See DeviceRuntime.wire_reflex_demo().
        self._safety_hook       = None
        self._intent_matcher    = IntentMatcher(sources_by_capability)
        self._raw_collector     = RawCollector()
        self._normalizer        = Normalizer()
        self._feature_extractor = FeatureExtractor()
        self._verdict_engine    = VerdictEngine()
        self._confidence_engine = ConfidenceEngine()

        self._seqs: dict[str, int] = defaultdict(int)   # per-resource sequence counters
        self._last_verdicts: dict[str, str | None] = {}  # for Part 2 event hook
        self._lock = threading.Lock()

    def set_event_emitter(self, fn) -> None:
        """Wire (or replace) the verdict-transition event sink after construction."""
        self._event_emitter = fn

    def set_safety_hook(self, fn) -> None:
        """Wire (or replace) the pre-return safety_check hook: callable(frame) ->
        frame. Used for a pre-return veto and/or a device-local reflex."""
        self._safety_hook = fn

    def handle(self, request: SenseRequest) -> SenseFrame:
        """
        Run the full forward pipeline for one SenseRequest.
        Returns a SenseFrame whose .data matches request.shape.
        """
        ts = time.time()

        # ── Part 2 (v1.3) — reflex fast-path. NAME-COLLISION NOTE ─────────────
        # The ORIGINAL reflex_path TODO meant a LATENCY optimization: skip
        # optional pipeline stages when mode=="urgent". The v1.3 "reflex path"
        # is a DIFFERENT feature — a device-LOCAL condition→action hook that runs
        # with no agent (see d2a/conditions.py + the EVENT LAYER). They share a
        # name only. The local-action reflex demo lands at the safety hook below
        # in v1.3 Phase 2; this urgent skip-stages optimization stays DEFERRED
        # (unbuilt — no speculative code).
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

        # ── Part 2 (v1.3) event hook — CLOSED: emit on verdict transition ─────
        # Fires as a "changed"-style edge: only on an actual transition, and
        # NEVER on the baseline (prev_verdict is None on the first sample for a
        # resource). Mirrors conditions.EdgeEvaluator's changed-op semantics.
        if (self._event_emitter is not None
                and prev_verdict is not None and verdict != prev_verdict):
            try:
                self._event_emitter("verdict_change", {
                    "resource": request.resource,
                    "from":     prev_verdict,
                    "to":       verdict,
                    "ts":       ts,
                })
            except Exception:
                pass

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

        # ── Part 2 (v1.3) safety_check() — CLOSED: pre-return hook ────────────
        # Final step before returning: may veto (set frame.vetoed / veto_reason)
        # or drive a device-LOCAL reflex (condition → local action, no agent).
        # No hook wired → frame passes through unchanged (Part 1 behavior).
        if self._safety_hook is not None:
            try:
                frame = self._safety_hook(frame) or frame
            except Exception:
                pass

        return frame
