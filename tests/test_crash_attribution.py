"""Restart attribution and low-end-only crash-floor evidence.

Regression coverage for the live 2026-11-21 incident: our own v2 limit writes
were counted as uncommanded crashes when a mining start edge arrived before
the Elapsed regression. High-power crashes must not invent a low-power floor.

Self-contained scaffolding (its own rig; see tests/ha_stubs.py).
"""
from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ha_stubs  # noqa: E402

if "homeassistant.helpers.storage" not in sys.modules:
    ha_stubs.install()

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "whatsminer"
if "wm" not in sys.modules:
    pkg = types.ModuleType("wm")
    pkg.__path__ = [str(PKG)]
    sys.modules["wm"] = pkg

controller_mod = importlib.import_module("wm.controller")
const = importlib.import_module("wm.const")

C = controller_mod
T0 = 1_700_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    def __init__(self, monkeypatch, **config):
        controller_mod.Store.saved.clear()
        controller_mod.persistent_notification.created.clear()
        controller_mod.persistent_notification.dismissed.clear()
        self.clock = Clock()
        monkeypatch.setattr(controller_mod, "time", self.clock)
        self.hass = ha_stubs.FakeHass()
        self.coord = ha_stubs.FakeCoordinator()
        self.coord.data["up_freq_finish"] = 1
        self.pid_state: dict = {}
        entry = types.SimpleNamespace(entry_id="e1")
        self.ctl = controller_mod.WhatsminerController(
            self.hass, entry, self.coord, self.pid_state, config
        )


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def seed_uptime(rig, uptime=300):
    rig.coord.data["uptime"] = uptime
    rig.ctl._note_uptime_and_crashes(True, rig.clock.t)


def regress_uptime(rig):
    """An Elapsed regression and its confirming poll, five minutes into a run."""
    rig.coord.data["uptime"] = 0
    rig.ctl._note_uptime_and_crashes(False, rig.clock.t)
    rig.clock.t += 30.0
    rig.coord.data["uptime"] = 30
    rig.ctl._note_uptime_and_crashes(True, rig.clock.t)


@pytest.mark.parametrize("restart_delay_s", [30.0, C.RESUME_BOOT_HOLD_S + 60.0])
def test_own_v2_limit_write_restart_is_not_crash_evidence(monkeypatch, restart_delay_s):
    """A start edge with stale Elapsed must not consume the write's ownership.

    The second delay is beyond the old ten-minute attribution window but
    inside the documented restart/tune latency.
    """
    rig = Rig(monkeypatch, **{const.CONF_POWER_MIN: 1000})
    rig.coord.data["wattage_limit"] = 1300
    seed_uptime(rig)

    async def go():
        assert await rig.ctl._set_power_limit(1300, why="test", safety=True)

    run_async(go())
    rig.clock.t += restart_delay_s
    # The start edge can be visible one poll before Elapsed resets. This used
    # to clear _pending_restart_at, making the next poll an uncommanded crash.
    rig.ctl._note_uptime_and_crashes(True, rig.clock.t)
    assert rig.ctl._claim_start(rig.clock.t) is True
    rig.clock.t += 30.0
    regress_uptime(rig)

    assert rig.ctl._last_restart_ours is True
    assert rig.ctl._crash_floor is None
    assert rig.ctl._crash_at is None
    assert "whatsminer_crash_floor" not in [
        k for k, _, _ in controller_mod.persistent_notification.created
    ]


def test_uncommanded_restart_at_power_min_still_raises_the_floor(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_POWER_MIN: 1000})
    rig.coord.data["wattage_limit"] = 1000
    seed_uptime(rig)
    rig.clock.t += 30.0

    regress_uptime(rig)

    assert rig.ctl._last_restart_ours is False
    assert rig.ctl._crash_floor == 1000 + C.CRASH_STEP_W
    assert rig.ctl._bounds(rig.clock.t)[0] == 1250


def test_own_v3_recycle_after_about_80s_is_not_crash_evidence(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_POWER_MIN: 1000})
    rig.coord.data["wattage_limit"] = 5000
    seed_uptime(rig)

    async def go():
        assert await rig.ctl._send_v3_power(1300, rig.clock.t, why="test")

    run_async(go())
    rig.clock.t += 80.0
    regress_uptime(rig)

    assert rig.ctl._last_restart_ours is True
    assert rig.ctl._crash_floor is None


def test_high_power_restart_does_not_raise_or_refresh_the_floor(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_POWER_MIN: 1000})
    rig.coord.data["wattage_limit"] = 4589
    seed_uptime(rig)
    rig.clock.t += 30.0

    regress_uptime(rig)

    assert rig.ctl._crash_floor is None
    assert rig.ctl._crash_at is None
    assert rig.ctl._bounds(rig.clock.t) == (1000, 5000)

    # High-power evidence cannot invert the floor over a fresh fault ceiling.
    rig.ctl._note_fault(rig.clock.t, 1200, "external derate to 1200W")
    assert rig.ctl._bounds(rig.clock.t) == (1000, 1450)
    assert rig.ctl._cap_inversion_logged is False


def test_low_power_crash_loop_ratchets_but_high_power_does_not_refresh_it(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_POWER_MIN: 1000})
    rig.ctl._note_crash(1000, rig.clock.t)
    rig.ctl._note_crash(1250, rig.clock.t + 30.0)  # at the learned floor
    learned_at = rig.ctl._crash_at
    assert rig.ctl._crash_floor == 1500

    rig.ctl._note_crash(4589, rig.clock.t + 60.0)
    assert rig.ctl._crash_floor == 1500
    assert rig.ctl._crash_at == learned_at
