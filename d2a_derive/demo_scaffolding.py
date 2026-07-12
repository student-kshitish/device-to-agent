"""
d2a_derive/demo_scaffolding.py — the `demo_odometry` Phase-2 scaffolding source.

Per ruling 1 (README protocol-gap #2): NO shipped capability exposes device
position, so `trajectory_free_space_map` has nothing real to bind. Rather than
pretend, we stand up an HONEST, clearly-named synthetic positional source used by
the derivation demo (and the live tests). This is a *capability-availability gap,
not an engine limitation* — the same executor binds the real `sensing` capability
for `thermal_ambient_proxy` unchanged.

What this provides:
  * OdometrySource — a SignalSource named "pose" (so the DataProvider flatten
    convention yields the recipe's `pose.x_m` / `pose.y_m` dotted fields) that
    walks a deterministic expanding-spiral trajectory, one step per read. Emits in
    a configurable unit (metres by default; centimetres exercises the executor's
    declared unit-adaptation path end-to-end).
  * demo_odometry_manifest — the OPEN-tier manifest ("synthetic trajectory for
    demonstration"), validated against d2a's manifest vocabulary.
  * register_demo_odometry(runtime) — attach the capability + source to a
    DeviceRuntime as an open, bindable capability (broker slot, policy allow,
    signed record). Call before start_swarm() and it publishes automatically; call
    after and it publishes immediately.

Nothing here is a reference recipe or a protocol change — it is demo/test
scaffolding for a capability the world does not yet ship.
"""

import math

from d2a import Capability
from d2a import manifest as _manifest
from d2a.stream_source import SignalSource

DEMO_ODOMETRY = "demo_odometry"
_DEMO_DESC = "synthetic trajectory for demonstration"


class OdometrySource(SignalSource):
    """Synthetic pose along a deterministic expanding spiral (new grid cells keep
    appearing, so a free-space map visibly grows). name='pose' → the frame's raw
    flattens to pose.x_m / pose.y_m. `unit` selects the emitted unit: 'm' (native)
    or 'cm' (values ×100, to drive the recipe's cm→m adaptation)."""

    name = "pose"

    def __init__(self, unit: str = "m", step_m: float = 0.25, turn: float = 0.5):
        self.unit = unit
        self._step_m = step_m
        self._turn = turn
        self._t = 0
        self._unit_factor = 100.0 if unit == "cm" else 1.0

    def read(self) -> dict | None:
        theta = self._t * self._turn
        r = self._step_m * self._t
        x_m = r * math.cos(theta)
        y_m = r * math.sin(theta)
        self._t += 1
        return {
            "x_m": round(x_m * self._unit_factor, 4),
            "y_m": round(y_m * self._unit_factor, 4),
        }

    def reset(self) -> None:
        self._t = 0


def demo_odometry_manifest(unit: str = "m") -> dict:
    """The open-tier manifest for the synthetic positional capability. Validated
    against d2a's manifest vocabulary (self-consistent open tier — demo_odometry
    is not a real sensitive resource, it is a labelled demonstration source)."""
    m = {
        "description": _DEMO_DESC,
        "reading": {
            "pose.x_m": {"type": "number", "unit": unit, "description": "x position"},
            "pose.y_m": {"type": "number", "unit": unit, "description": "y position"},
        },
        "consent_tier": "open",
        "streaming": True,
    }
    return _manifest.validate_manifest(m, "open")


def register_demo_odometry(runtime, unit: str = "m", *, source: OdometrySource | None = None,
                           allow: bool = True) -> OdometrySource:
    """Attach `demo_odometry` to a DeviceRuntime as an open, bindable capability.

    Registers the Capability (with its manifest), a broker quota slot, a policy
    `allow` rule (open demo source), and the OdometrySource behind the DataProvider.
    If the runtime's swarm is already started, publishes the signed record now;
    otherwise start_swarm() will publish it (it is now in `capabilities`). Returns
    the source so a caller can reset/inspect it.
    """
    src = source if source is not None else OdometrySource(unit=unit)
    man = demo_odometry_manifest(unit)

    cap = Capability(
        name=DEMO_ODOMETRY,
        tags=[DEMO_ODOMETRY, "open", "demo"],
        live_state={"synthetic": True, "unit": unit},
        node_id=runtime.node_id,
        public_key=runtime.public_key,
        manifest=man,
    )
    runtime.capabilities[DEMO_ODOMETRY] = cap
    runtime.broker.quotas[DEMO_ODOMETRY] = 1
    if allow:
        runtime.policy.allow(DEMO_ODOMETRY)

    # place the source behind the DataProvider (get_reading + the streaming loop
    # both read it fresh, exactly like a hardware source).
    runtime.data._sources[DEMO_ODOMETRY] = [src]

    # publish now if the swarm is up; otherwise start_swarm() covers it.
    try:
        ip, port = runtime.swarm.address
        runtime.swarm.publish(runtime._capability_record(cap, ip, port))
    except Exception:
        pass
    return src
