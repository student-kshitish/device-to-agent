from __future__ import annotations
from typing import Callable


class ReleaseManager:
    """
    Stage 10 — Release Manager.
    Releases every binding in a composition atomically.
    Idempotent: never throws, handles already-expired bindings gracefully.
    After release_all(), the composition holds zero live bindings.
    """

    def __init__(self, release_fn: Callable):
        # release_fn(binding_dict) -> None
        self._release_fn = release_fn

    def release_all(self, composition) -> dict:
        """
        Release every binding, stop monitor daemon, clear binding list.
        Returns {"released": [...], "errors": [...], "ok": bool}.
        """
        # Stop monitor daemon first (no background threads linger)
        try:
            composition.monitor.stop_monitoring()
        except Exception:
            pass

        released = []
        errors   = []

        for binding in list(composition.bindings):
            if not binding:
                continue
            node_id  = binding.get("provider_node_id", "?")
            cap_name = binding.get("capability_name", "?")
            try:
                self._release_fn(binding)
                released.append(f"{node_id}/{cap_name}")
            except Exception as exc:
                errors.append(f"{node_id}/{cap_name}: {exc}")

        # Clear binding list so double-release is safe
        composition.bindings.clear()

        return {
            "released": released,
            "errors":   errors,
            "ok":       len(errors) == 0,
        }
