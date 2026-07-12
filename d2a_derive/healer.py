"""
d2a_derive/healer.py — self-healing for a live DerivedCapability.

A derived capability is only as alive as its inputs' leases. When one dies, the
healer decides what to do FROM THE LOSS CODE (the unified error model's
LeaseLostError.code), because the two ways a lease dies mean very different things:

  lease_expired (or any renew failure / version / trust loss)
      The device is still there; the lease just lapsed. RECOVER: rediscover the
      provider, rebind under a fresh lease, and re-arm the feed (re-subscribe or
      restart the pull loop). Bounded attempts with backoff — never a busy-spin.
      Exhausting the attempts marks the input gone.

  device_shutdown
      The device ANNOUNCED it is leaving (graceful departure). Retrying hard and
      fast is wrong — it told us it's gone. Mark the input gone IMMEDIATELY (so the
      capability reflects reality now), then attempt rediscovery on a LONGER
      schedule in case the device comes back ("resume"). The first retry is
      deliberately delayed — the distinguishing behaviour from lease_expired.

When an input is permanently unrecoverable the derived capability goes FAILED if
the input was required, DEGRADED if the recipe marked it optional. Either way the
per-input state flips to "gone" and DerivedCapability.on_state_change fires with
old/new/reason.

NO BUSY-SPIN: every wait is a stop-aware Event.wait(backoff); tests assert the
bound via attempt count and elapsed time. All branches log.
"""

import threading
import time

from d2a import errors
from d2a_derive.validator import check_input_against_provider

# Loss codes that mean "the device is still there, just rebind". Everything that
# is NOT an announced departure falls here (lease lapse, a denied/stale renew, a
# version or trust loss) — the conservative default is to try to recover.
_ANNOUNCED_DEPARTURE = errors.DEVICE_SHUTDOWN


class SelfHealer:
    def __init__(self, dc, *, max_attempts: int = 4, backoff_s: float = 0.2,
                 shutdown_backoff_s: float = 2.0):
        self.dc = dc
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.shutdown_backoff_s = shutdown_backoff_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ── entry point ──────────────────────────────────────────────────────────────

    def on_loss(self, feed, code: str) -> None:
        """Called (from the agent renew thread, the pull loop, or a resync) the
        moment an input's lease is lost. Coalesces concurrent losses for the same
        input into ONE heal thread, then branches on the loss code."""
        dc = self.dc
        with dc._lock:
            if dc._closed or feed._healing:
                return
            feed._healing = True

        threading.Thread(target=self._heal, args=(feed, code), daemon=True,
                         name=f"heal-{feed.hint}").start()

    # ── the heal loop ────────────────────────────────────────────────────────────

    def _heal(self, feed, code: str) -> None:
        dc = self.dc
        announced = (code == _ANNOUNCED_DEPARTURE)

        if announced:
            # The device SAID it's leaving. Reflect that now: mark the input gone
            # (→ required: FAILED, optional: DEGRADED) BEFORE any retry, and do the
            # first rediscovery only after a longer wait — no immediate retry.
            print(f"[heal:{dc.provided_name}] input '{feed.hint}' device_shutdown "
                  f"→ marked gone; slow rediscovery (no immediate retry)")
            with dc._lock:
                feed.state = "gone"
                dc._recompute_state_locked("device_shutdown")
            base_backoff = self.shutdown_backoff_s
        else:
            # Recoverable lapse. Show the capability as degraded WHILE we rebind, so
            # health() honestly reports "not currently healthy, working on it".
            print(f"[heal:{dc.provided_name}] input '{feed.hint}' lost ({code}) "
                  f"→ rebinding (bounded, backoff)")
            with dc._lock:
                feed.state = "rebinding"
                dc._recompute_state_locked(code)
            base_backoff = self.backoff_s

        try:
            self._attempt_recovery(feed, base_backoff, announced)
        finally:
            with dc._lock:
                feed._healing = False

    def _attempt_recovery(self, feed, base_backoff: float, announced: bool) -> None:
        """Bounded rediscover→rebind→re-arm with exponential-ish backoff. Returns
        when recovered or after max_attempts (leaving the input gone)."""
        dc = self.dc
        for attempt in range(1, self.max_attempts + 1):
            # Wait FIRST — on an announced departure this is what makes the first
            # retry non-immediate; on a lapse it spaces bounded attempts. Stop-aware
            # so close() interrupts it (never a busy-spin).
            wait = base_backoff * attempt
            if self._stop.wait(wait):
                return
            with dc._lock:
                if dc._closed:
                    return

            provider = self._rediscover(feed)
            if provider is None:
                print(f"[heal:{dc.provided_name}] input '{feed.hint}' attempt "
                      f"{attempt}/{self.max_attempts}: no provider yet")
                continue

            # best-effort: drop any stale stream handler for the dead binding so no
            # orphaned routing lingers, then rebind + re-arm on the fresh lease.
            self._drop_stale_stream(feed)
            with dc._lock:
                feed.provider = provider
            if not dc._bind_feed(feed):
                print(f"[heal:{dc.provided_name}] input '{feed.hint}' attempt "
                      f"{attempt}/{self.max_attempts}: rebind not verified")
                continue

            with dc._lock:
                if dc._closed:
                    return
                feed.rebind_count += 1
                feed.state = "active"
                dc._recompute_state_locked("recovered")
            dc._start_feed(feed)
            print(f"[heal:{dc.provided_name}] input '{feed.hint}' RECOVERED on "
                  f"attempt {attempt} (rebind_count={feed.rebind_count})")
            return

        # exhausted — the input is unrecoverable for now.
        with dc._lock:
            if dc._closed:
                return
            feed.state = "gone"
            dc._recompute_state_locked("unrecoverable")
        kind = "optional → degraded" if feed.optional else "required → failed"
        print(f"[heal:{dc.provided_name}] input '{feed.hint}' unrecoverable after "
              f"{self.max_attempts} attempts ({kind})")

    # ── helpers ──────────────────────────────────────────────────────────────────

    def _rediscover(self, feed):
        """Rediscover a provider that still satisfies the recipe's contract for this
        input. Prefer the original node; accept any manifest-carrying provider whose
        manifest passes the same contract check the planner used. NETWORK op."""
        try:
            records = self.agent().find_capability(feed.hint) or []
        except Exception:
            records = []

        orig_node = feed.provider.get("node_id")
        # original node first, then the rest — stable recovery to the same device.
        records = sorted(records, key=lambda r: 0 if r.get("node_id") == orig_node else 1)

        req = self._req_for(feed.hint)
        for rec in records:
            man = rec.get("manifest")
            if not isinstance(man, dict):
                continue
            ok, _ = check_input_against_provider(req, man, self.dc.recipe.unit_adaptations)
            if ok:
                return {"node_id": rec.get("node_id"), "name": rec.get("name"),
                        "manifest": man}
        return None

    def _req_for(self, hint: str) -> dict:
        for req in self.dc.recipe.requires:
            if req.get("capability_hint") == hint:
                return req
        return {}

    def _drop_stale_stream(self, feed) -> None:
        """Forget the agent-side stream handler keyed by the dead binding so a
        late frame can't route into us after rebind. Best-effort, no wire I/O."""
        agent = self.agent()
        bid = (feed.binding or {}).get("binding_id")
        if not bid:
            return
        try:
            agent._stream_handlers.pop(bid, None)
            agent._stream_sub_ids.pop(bid, None)
        except Exception:
            pass

    def agent(self):
        return self.dc.agent
