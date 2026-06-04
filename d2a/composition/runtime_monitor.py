from __future__ import annotations
import threading
from typing import Callable


class RuntimeMonitor:
    """
    Stage 9 — Runtime Monitor.
    Polls health of every bound stage on demand (check()) and optionally
    as a daemon loop (start_monitoring / stop_monitoring).
    Daemon is OFF by default — opt in only.

    health_fn(binding_dict) -> {"verdict": str, "healthy": bool}
      - verdict "distress" or "error" or "expired" → unhealthy
      - binding expired/preempted → unhealthy
    """

    def __init__(self, health_fn: Callable):
        self._health_fn = health_fn
        self._daemon_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def check(self, composition) -> dict:
        """
        Poll health of every bound stage.
        Returns per-stage {verdict, healthy} and overall_healthy flag.
        """
        results: dict = {}
        overall_healthy = True

        for i, (hop, binding) in enumerate(
            zip(composition.bound_blueprint.hops, composition.bindings)
        ):
            key = f"hop_{i}_{hop.node_id[:12]}"
            try:
                health = self._health_fn(binding)
            except Exception as exc:
                health = {"verdict": "error", "healthy": False, "error": str(exc)}

            verdict    = health.get("verdict", "unknown")
            is_healthy = health.get("healthy", verdict not in ("distress", "error", "expired"))
            results[key] = {"verdict": verdict, "healthy": is_healthy}

            if not is_healthy:
                overall_healthy = False

        results["overall_healthy"] = overall_healthy
        return results

    def on_unhealthy(self, composition) -> str:
        """Recommend next action. "fallback" if backups exist, else "abort"."""
        if composition._remaining_fallbacks:
            return "fallback"
        return "abort"

    def start_monitoring(
        self,
        composition,
        interval_s: float,
        callback: Callable,
    ) -> None:
        """
        Start optional daemon monitor (opt-in).
        Calls callback(health_result) whenever a check finds an unhealthy stage.
        """
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.wait(interval_s):
                try:
                    result = self.check(composition)
                    if not result.get("overall_healthy", True):
                        callback(result)
                except Exception:
                    pass

        self._daemon_thread = threading.Thread(
            target=_loop, daemon=True, name="d2a-monitor"
        )
        self._daemon_thread.start()

    def stop_monitoring(self) -> None:
        """Stop the daemon loop."""
        self._stop_event.set()
        if self._daemon_thread and self._daemon_thread.is_alive():
            self._daemon_thread.join(timeout=2.0)
        self._daemon_thread = None
