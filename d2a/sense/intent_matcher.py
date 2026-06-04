"""
d2a/sense/intent_matcher.py — IntentMatcher: resource name → signal sources.

Agents name resources (e.g. "compute", "battery_aware"). They NEVER name /proc
paths or sysfs files. This module is the single place that knows which physical
signals back each resource on this specific device.

Transparent by design: explain() returns a human-readable string so callers
can log exactly what will be read without inspecting the source objects.
"""

from d2a.sense_types import SenseRequest


class IntentMatcher:
    """
    Resolves an agent's resource request to the concrete SignalSources registered
    for that resource by DeviceRuntime._build_sources().

    sources_by_capability is the same dict DeviceRuntime builds:
      {capability_name: [SignalSource, ...]}

    If a resource is not offered on this device, resolve() returns [] — the
    caller handles that as "not offered" without this module raising.
    """

    def __init__(self, sources_by_capability: dict) -> None:
        self._sources = sources_by_capability

    def resolve(self, request: SenseRequest) -> list:
        """
        Return the list of SignalSources for this resource.
        Returns [] if the resource is not registered on this device.
        """
        return list(self._sources.get(request.resource, []))

    def explain(self, request: SenseRequest) -> str:
        """Human-readable one-liner for logging and transparency."""
        sources = self.resolve(request)
        if not sources:
            return f"'{request.resource}' not offered on this device (no sources registered)"
        names = [s.name for s in sources]
        return (
            f"resource='{request.resource}' → sources={names} "
            f"shape={request.shape!r} mode={request.mode!r}"
        )
