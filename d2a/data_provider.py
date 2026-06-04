"""
d2a/data_provider.py — two-mode data engine for a device.

DEFAULT  = on-demand pull (get_reading).   Zero background work.
OPT-IN   = streaming     (subscribe).      Daemon thread per capability,
           started only on first subscribe, stopped when last subscriber leaves.
"""

import threading
import time
import uuid
from collections import defaultdict

from d2a.stream_source import SignalSource
from d2a.preprocessor import Preprocessor


class DataProvider:
    """
    sources_by_capability: maps capability name -> list of SignalSource instances.
    One Preprocessor per capability; shared by on-demand and streaming reads so
    the ring buffer sees all readings for better delta/rate computation.
    """

    def __init__(self, sources_by_capability: dict[str, list]) -> None:
        self._sources:       dict[str, list[SignalSource]] = sources_by_capability
        self._preprocessors: dict[str, Preprocessor]      = {
            cap: Preprocessor() for cap in sources_by_capability
        }

        # streaming state — all guarded by self._lock
        self._subs:         dict[str, dict]       = {}               # sub_id -> {cap, callback, hz}
        self._subs_by_cap:  dict[str, set]        = defaultdict(set) # cap -> {sub_id, ...}
        self._loop_threads: dict[str, threading.Thread] = {}         # cap -> Thread
        self._loop_running: dict[str, bool]       = {}               # cap -> bool
        self._lock = threading.Lock()

        # stats counters (defaultdict so missing keys read as 0)
        self._on_demand_reads:   dict[str, int] = defaultdict(int)
        self._stream_frames_sent: dict[str, int] = defaultdict(int)

    # ── internal read ──────────────────────────────────────────────────────────

    def _get_frame(self, capability_name: str) -> dict:
        """Read all sources fresh right now, build and return one structured frame.
        Does NOT increment on_demand_reads — used internally by the streaming loop."""
        sources = self._sources.get(capability_name, [])
        raw: dict = {}
        for src in sources:
            try:
                result = src.read()
                if result is not None:
                    raw[src.name] = result
            except Exception:
                pass

        pp = self._preprocessors.setdefault(capability_name, Preprocessor())
        return pp.make_frame(raw)

    # ── DEFAULT PATH ───────────────────────────────────────────────────────────

    def get_reading(self, capability_name: str) -> dict:
        """
        THE DEFAULT PATH.
        Reads all sources for this capability FRESH right now, builds and returns
        ONE structured frame. Pure pull — no threads, no background work.
        Call this when an agent needs the data at a specific moment.
        """
        frame = self._get_frame(capability_name)
        self._on_demand_reads[capability_name] += 1
        return frame

    # ── OPT-IN STREAMING ──────────────────────────────────────────────────────

    def subscribe(self, capability_name: str, callback, hz: float = 5.0) -> str:
        """
        OPT-IN streaming. Lazily starts a daemon thread on first subscribe for
        this capability; the thread calls get_reading at `hz` and pushes each
        frame to every registered callback. Multiple subscribers share one loop.
        Returns sub_id for use with unsubscribe().
        """
        sub_id = str(uuid.uuid4())

        with self._lock:
            self._subs[sub_id] = {"cap": capability_name, "callback": callback, "hz": hz}
            self._subs_by_cap[capability_name].add(sub_id)

            # start loop thread only if not already running
            already_running = (
                capability_name in self._loop_threads
                and self._loop_threads[capability_name].is_alive()
                and self._loop_running.get(capability_name, False)
            )
            if not already_running:
                self._loop_running[capability_name] = True
                t = threading.Thread(
                    target=self._stream_loop,
                    args=(capability_name,),
                    daemon=True,
                    name=f"d2a-stream-{capability_name}",
                )
                self._loop_threads[capability_name] = t
                t.start()

        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        """
        Remove a subscriber. If no subscribers remain for a capability,
        stop its loop thread (back to zero background work).
        """
        with self._lock:
            info = self._subs.pop(sub_id, None)
            if info is None:
                return
            cap = info["cap"]
            self._subs_by_cap[cap].discard(sub_id)
            if not self._subs_by_cap[cap]:
                # signal the loop thread to exit on its next iteration
                self._loop_running[cap] = False

    def _stream_loop(self, capability_name: str) -> None:
        """Daemon loop: reads fresh data at the highest requested hz and pushes
        frames to all subscribers for this capability. Exits when no subscribers remain."""
        while self._loop_running.get(capability_name, False):
            with self._lock:
                sub_ids = set(self._subs_by_cap.get(capability_name, set()))

            if not sub_ids:
                break

            # run at the fastest hz among current subscribers
            with self._lock:
                hz_list = [
                    self._subs[sid]["hz"]
                    for sid in sub_ids
                    if sid in self._subs
                ]
            if not hz_list:
                break

            hz       = max(hz_list)
            interval = 1.0 / max(hz, 0.1)

            # fresh read (does not count toward on_demand_reads)
            frame = self._get_frame(capability_name)

            with self._lock:
                callbacks = [
                    self._subs[sid]["callback"]
                    for sid in list(self._subs_by_cap.get(capability_name, set()))
                    if sid in self._subs
                ]

            for cb in callbacks:
                try:
                    cb(frame)
                except Exception:
                    pass

            # count once per loop iteration (one frame generated, pushed to all)
            self._stream_frames_sent[capability_name] += 1

            time.sleep(interval)

    # ── stats ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Per capability: on_demand_reads, stream_frames_sent, active_subscribers."""
        with self._lock:
            all_caps = (
                set(self._sources)
                | set(self._on_demand_reads)
                | set(self._stream_frames_sent)
            )
            return {
                cap: {
                    "on_demand_reads":    self._on_demand_reads.get(cap, 0),
                    "stream_frames_sent": self._stream_frames_sent.get(cap, 0),
                    "active_subscribers": len(self._subs_by_cap.get(cap, set())),
                }
                for cap in sorted(all_caps)
            }
