"""
d2a/interventions.py — PHASE 8: the MUTATING executors (the fix, not the observe).

Unlike d2a/stream_source.py (read-only signal sources) and the Phase 7 diagnostics,
an intervention CHANGES device state to repair it. A wrong intervention can worsen
things with NO undo, so the safety of this layer does NOT live here — it lives in
the device runtime's DOUBLE GATE (bind approval + per-plan owner approval) and the
signed audit trail. This module is only the hands: bounded, never-raising executors
that perform ONE mutation and report honestly whether it worked.

Each executor:
  - preflight() -> (ok: bool, reason: str): a cheap pre-mutation check (privilege,
    tool availability). A False here means propose_intervention returns
    `refused_preflight` and NOTHING is mutated.
  - execute(action, params) -> {"ok": bool, "detail": str, "code": str}: performs
    the mutation via a standard tool / syscall with a timeout. Never raises — any
    failure becomes ok=False with an honest detail.

LEAF MODULE: pure stdlib. It does not know about consent, approval, or audit —
those are the runtime's job. Reversibility is a per-PLAN property (declared in the
InterventionPlan), not a property of the executor.
"""

import os
import signal
import shutil
import subprocess
import time

_SUBPROC_TIMEOUT = 10.0   # a stuck fixer must not wedge the device handler


def _pid_alive(pid: int) -> bool:
    """True if `pid` still exists. os.kill(pid, 0) probes without signalling."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True    # exists but owned by another user


class Intervention:
    """Base mutating executor. Subclasses set `family` and implement _run()."""

    family: str = "intervention"

    def preflight(self) -> tuple[bool, str]:
        """Cheap pre-mutation check. Default: always ready. Override to gate on
        privilege / tool availability so a doomed mutation is refused BEFORE it
        half-runs."""
        return True, ""

    def actions(self) -> set[str]:
        raise NotImplementedError

    def diagnostic_kwargs(self) -> dict:
        """Kwargs the PAIRED diagnostic source needs to observe the SAME thing this
        executor mutates (e.g. user-scope for a --user service). The device's verify
        reads the paired diagnostic with these so scope can never drift between the
        fix and its check. Default: none."""
        return {}

    def execute(self, action: str, params: dict) -> dict:
        """Never-raise firewall around the concrete mutation."""
        if action not in self.actions():
            return {"ok": False, "detail": f"unknown action {action!r}", "code": "invalid_plan"}
        try:
            return self._run(action, params or {})
        except Exception as e:
            return {"ok": False, "detail": f"execute failed: {type(e).__name__}: {e}",
                    "code": "intervention_error"}

    def _run(self, action: str, params: dict) -> dict:
        raise NotImplementedError


class ServiceIntervene(Intervention):
    """Start / stop / restart a systemd unit (user-scope by default — no root).
    Reversible (stop<->start). Paired diagnostic: service_health."""

    family = "service_intervene"

    def __init__(self, unit: str, user: bool = True) -> None:
        self.unit = unit
        self.user = user

    def actions(self) -> set[str]:
        return {"start", "stop", "restart"}

    def diagnostic_kwargs(self) -> dict:
        # service_health must read the SAME scope this fixer mutates.
        return {"user": self.user}

    def preflight(self) -> tuple[bool, str]:
        if not shutil.which("systemctl"):
            return False, "systemctl absent — not a systemd host"
        return True, ""

    def _run(self, action: str, params: dict) -> dict:
        scope = ["--user"] if self.user else []
        r = subprocess.run(["systemctl", *scope, action, self.unit],
                           capture_output=True, text=True, timeout=_SUBPROC_TIMEOUT)
        if r.returncode == 0:
            return {"ok": True, "detail": f"systemctl {' '.join(scope)} {action} {self.unit}",
                    "code": ""}
        return {"ok": False, "code": "intervention_error",
                "detail": (r.stderr or r.stdout).strip()[:200] or f"rc={r.returncode}"}


class ProcessRelease(Intervention):
    """Release a device node by signalling the PID holding it (default SIGTERM).
    IRREVERSIBLE — a kill has no undo. Paired diagnostic: device_node_health."""

    family = "process_release"

    _SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "HUP": signal.SIGHUP}

    def __init__(self, node_path: str) -> None:
        self.node_path = node_path

    def actions(self) -> set[str]:
        return {"release"}

    def _run(self, action: str, params: dict) -> dict:
        pid = params.get("pid")
        if not isinstance(pid, (int, float)) or isinstance(pid, bool) or int(pid) <= 0:
            return {"ok": False, "detail": f"pid must be a positive integer, got {pid!r}",
                    "code": "invalid_plan"}
        pid = int(pid)
        sig_name = str(params.get("signal", "TERM")).upper()
        sig = self._SIGNALS.get(sig_name)
        if sig is None:
            return {"ok": False, "detail": f"unknown signal {sig_name!r}", "code": "invalid_plan"}
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            # Already gone → the node is released, which is the intended outcome.
            return {"ok": True, "detail": f"pid {pid} already gone (node released)", "code": ""}
        except PermissionError:
            return {"ok": False, "code": "intervention_error",
                    "detail": f"insufficient privilege to signal pid {pid}"}
        # A signal is async: the release is not complete until the holder actually
        # exits. Wait briefly (bounded) so the device-run VERIFY sees settled state.
        deadline = time.time() + 3.0
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.02)
        gone = not _pid_alive(pid)
        return {"ok": True, "code": "",
                "detail": f"sent SIG{sig_name} to pid {pid}"
                          + ("" if gone else " (still exiting)")}


class KernelModuleIntervene(Intervention):
    """Load / unload a kernel module (modprobe). Reversible (load<->unload).
    REQUIRES privilege (CAP_SYS_MODULE / root) — refused at preflight otherwise, so
    it never half-mutates on an unprivileged host. Paired diagnostic:
    kernel_module_health."""

    family = "kernel_module_intervene"

    def __init__(self, module: str) -> None:
        self.module = module

    def actions(self) -> set[str]:
        return {"load", "unload"}

    def preflight(self) -> tuple[bool, str]:
        if not shutil.which("modprobe"):
            return False, "modprobe absent"
        # Loading/unloading a module needs CAP_SYS_MODULE; euid 0 is the portable
        # proxy. On this repo's dev machine euid=1000, so this refuses cleanly
        # rather than failing mid-modprobe.
        if os.geteuid() != 0:
            return False, "requires CAP_SYS_MODULE / root"
        return True, ""

    def _run(self, action: str, params: dict) -> dict:
        cmd = ["modprobe", "-r", self.module] if action == "unload" else ["modprobe", self.module]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_SUBPROC_TIMEOUT)
        if r.returncode == 0:
            return {"ok": True, "detail": " ".join(cmd), "code": ""}
        return {"ok": False, "code": "intervention_error",
                "detail": (r.stderr or r.stdout).strip()[:200] or f"rc={r.returncode}"}


# family → executor class. attach_intervention builds one via cls(target, **opts);
# the target is always the first positional arg (unit / node_path / module).
INTERVENTION_EXECUTORS: dict[str, type] = {
    "service_intervene":       ServiceIntervene,
    "process_release":         ProcessRelease,
    "kernel_module_intervene": KernelModuleIntervene,
}
