"""
d2a/policy.py — owner-consent resource policy.

Safe by default: sensitive resources (camera, mic, location, display) require
explicit owner opt-in before any remote agent can bind them.
Open resources (compute, gpu, storage, network, sensing) are bindable by any
trusted remote agent without additional approval.

Usage:
    policy = ResourcePolicy()                  # all defaults; sensitive = needs approval
    policy.allow("camera")                     # owner explicitly opens camera
    policy.deny("microphone")                  # owner blocks microphone entirely
    policy.set_approval_callback(fn)           # fn(resource, agent_id) -> bool for case-by-case

    DeviceRuntime(name="x")                    # safe defaults automatically
    DeviceRuntime(name="x", open_resources=["camera"])  # owner opens camera
"""

from d2a.resource_probes import RESOURCE_SENSITIVITY


class ResourcePolicy:
    """
    Per-device bind policy derived from RESOURCE_SENSITIVITY defaults.

    Default rules:
      OPEN      resources → "allow"          (any remote agent may bind)
      sensitive resources → "needs_approval" (requires owner consent)

    Local (same-process) binds are always allowed regardless of rules,
    since same-process code is already inside the trust boundary.
    """

    def __init__(self, device_class: str = None) -> None:
        # Build default rules from sensitivity classification.
        self._rules: dict[str, str] = {}
        for name, sensitivity in RESOURCE_SENSITIVITY.items():
            self._rules[name] = "allow" if sensitivity == "open" else "needs_approval"

        # Default approval callback: DENY (safe). Owner replaces this with a real prompt.
        self._approval_callback = None
        self._device_class = device_class

    # ── owner API ─────────────────────────────────────────────────────────────

    def allow(self, resource_name: str) -> None:
        """Owner explicitly marks a resource bindable by remote agents."""
        self._rules[resource_name] = "allow"

    def deny(self, resource_name: str) -> None:
        """Owner blocks a resource entirely — no remote agent can bind."""
        self._rules[resource_name] = "deny"

    def require_approval(self, resource_name: str) -> None:
        """Each bind request for this resource requires approval callback confirmation."""
        self._rules[resource_name] = "needs_approval"

    def set_approval_callback(self, fn) -> None:
        """
        Set approval callback: fn(resource_name, agent_id) -> bool.
        Called when check() returns "needs_approval".
        Default (no callback set) = DENY for safety.
        An owner-facing app should wire a prompt here.
        """
        self._approval_callback = fn

    # ── policy evaluation ──────────────────────────────────────────────────────

    def check(self, resource_name: str, agent_id: str, is_remote: bool) -> str:
        """
        Return "allow", "deny", or "needs_approval" for a bind attempt.

        Local binds (is_remote=False, same process/device) are always allowed.
        Remote binds (is_remote=True, from network) follow the rule:
          open      → "allow"
          sensitive → "needs_approval" unless owner called allow()
          denied    → "deny"
        Unknown resource → "needs_approval" (unknown = sensitive, safe default).
        """
        if not is_remote:
            return "allow"
        return self._rules.get(resource_name, "needs_approval")

    def approve(self, resource_name: str, agent_id: str) -> bool:
        """
        Call the approval callback for a resource that needs_approval.
        Returns False (safe default) if no callback is set.
        """
        if self._approval_callback is None:
            return False
        try:
            return bool(self._approval_callback(resource_name, agent_id))
        except Exception:
            return False

    # ── introspection ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "rules":                  dict(self._rules),
            "approval_callback_set":  self._approval_callback is not None,
            "device_class":           self._device_class,
        }

    def summary(self) -> dict:
        """Return rules grouped by outcome — useful for printing policy state."""
        groups: dict = {"open": [], "needs_approval": [], "denied": []}
        for res, rule in sorted(self._rules.items()):
            if rule == "allow":
                groups["open"].append(res)
            elif rule == "needs_approval":
                groups["needs_approval"].append(res)
            elif rule == "deny":
                groups["denied"].append(res)
        return groups
