"""
Test isolation for D2A's persisted crypto state.

Every test module that constructs a DeviceRuntime or RemoteAgent must point
D2A_HOME at a throwaway tmpdir BEFORE those objects are built, so the suite
never reads or writes the real ~/.d2a (keys + TOFU pins). Use:

    from tests._env import use_tmp_home, restore_home
    def setUpModule():   use_tmp_home()
    def tearDownModule(): restore_home()
"""

import os
import shutil
import tempfile

_state: dict = {}


def use_tmp_home() -> str:
    d = tempfile.mkdtemp(prefix="d2a-test-home-")
    _state["prev"] = os.environ.get("D2A_HOME")
    _state["dir"] = d
    os.environ["D2A_HOME"] = d
    return d


def restore_home() -> None:
    prev = _state.get("prev")
    if prev is None:
        os.environ.pop("D2A_HOME", None)
    else:
        os.environ["D2A_HOME"] = prev
    shutil.rmtree(_state.get("dir", ""), ignore_errors=True)
    _state.clear()
