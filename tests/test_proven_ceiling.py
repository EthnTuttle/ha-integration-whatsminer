"""Fault bounds (v2.0): one always-on bounds source, ``_bounds(now) -> (floor, ceiling)``.

Ceiling: Power Max, unless fault evidence (error code, external derate,
hashrate-collapse undo, hold taint) is fresh (FAULT_TTL_S) — then the highest
witnessed hold (else the power that faulted, else Power Min) + PROVEN_MARGIN_W,
clamped to [Power Min, Power Max]. Floor: Power Min, or the last uncommanded
short run's power + CRASH_STEP_W while fresh. Inversion: the ceiling wins.

A mining run holding one power for STABLE_RUN_S witnesses it as proven_max,
unless the hold is tainted or the miner is externally derated; the proof
timestamp refreshes at most hourly. Reset Fault Memory forgets all of it.

Self-contained scaffolding (its own rig; see tests/ha_stubs.py).
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ha_stubs  # noqa: E402

ha_stubs.install()

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "whatsminer"
pkg = types.ModuleType("wm")
pkg.__path__ = [str(PKG)]
sys.modules.setdefault("wm", pkg)
controller_mod = importlib.import_module("wm.controller")
pn = sys.modules["homeassistant.components.persistent_notification"]
Store = sys.modules["homeassistant.helpers.storage"].Store

C = controller_mod
T0 = 1_700_000_000.0
KEY = "whatsminer.e1.controller"
MARGIN = C.PROVEN_MARGIN_W  # 250
TTL = C.FAULT_TTL_S  # 7 days


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """A controller plus the fakes around it (no sensors needed: the bounds
    tests drive `_note_*`/`_bounds` directly, never the full tick)."""

    def __init__(self, monkeypatch):
        Store.saved.clear()
        pn.created.clear()
        pn.dismissed.clear()
        self.clock = Clock()
        monkeypatch.setattr(controller_mod, "time", self.clock)
        self.hass = ha_stubs.FakeHass()
        self.coord = ha_stubs.FakeCoordinator()
        self.pid_state: dict = {}
        entry = types.SimpleNamespace(entry_id="e1")
        self.ctl = controller_mod.WhatsminerController(
            self.hass, entry, self.coord, self.pid_state, {}
        )

    @property
    def calls(self):
        return self.coord.api.calls


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _faults(ctl, rig):
    ctl._publish_faults()
    return rig.pid_state["faults"]


# ----------------------------------------------------------------- unarmed


def test_no_fault_evidence_gives_the_configured_bounds(monkeypatch):
    rig = Rig(monkeypatch)
    assert rig.ctl._bounds(rig.clock.t) == (1000, 5000)  # (Power Min, Power Max)
    faults = rig.pid_state["faults"]
    assert faults["floor_w"] == 1000 and faults["ceiling_w"] == 5000
    assert faults["fault_at"] is None and faults["proven_max_w"] is None
    assert faults["crash_floor_w"] is None


# ----------------------------------------------------------------- witness


def test_a_stable_hold_witnesses_the_held_power(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._hold_power, ctl._hold_since = 3000, rig.clock.t

    ctl._settle_hold(rig.clock.t + C.STABLE_RUN_S - 60)  # under a full run: nothing
    assert ctl._proven_max is None
    ctl._settle_hold(rig.clock.t + C.STABLE_RUN_S)
    assert ctl._proven_max == 3000
    assert ctl._proven_max_at == rig.clock.t + C.STABLE_RUN_S


def test_witnessing_is_monotonic(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._hold_power, ctl._hold_since = 3500, rig.clock.t
    ctl._settle_hold(rig.clock.t + C.STABLE_RUN_S)
    assert ctl._proven_max == 3500
    ctl._hold_power, ctl._hold_since = 2000, rig.clock.t
    ctl._settle_hold(rig.clock.t + C.STABLE_RUN_S * 2)  # a lower hold lowers nothing
    assert ctl._proven_max == 3500


def test_a_tainted_hold_witnesses_nothing(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._hold_power, ctl._hold_since = 3000, rig.clock.t
    ctl._note_fault(rig.clock.t, 3000, "error code 2320")  # taints the run in progress
    ctl._settle_hold(rig.clock.t + C.STABLE_RUN_S * 2)
    assert ctl._proven_max is None
    ctl._note_fault(rig.clock.t, 3000, "error code 2320")
    assert _faults(ctl, rig)["fault_power_w"] == 3000


def test_an_externally_derated_hold_witnesses_nothing(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._hold_power, ctl._hold_since = 3000, rig.clock.t
    ctl._external_derate = True  # what it holds now is its choice, not our ask
    ctl._settle_hold(rig.clock.t + C.STABLE_RUN_S * 2)
    assert ctl._proven_max is None


def test_our_next_command_marks_a_clean_run_boundary(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._hold_power, ctl._hold_since = 3000, rig.clock.t - 3600  # a long hold...
    ctl._note_fault(rig.clock.t, 3000, "error code 2320")  # ...now tainted

    async def go():
        assert await ctl._set_power_limit(3100, why="test")

    run_async(go())
    # The tainted long hold was settled without credit at the boundary; the new
    # segment starts clean at the power just put in force.
    assert ctl._proven_max is None
    assert ctl._hold_tainted is False
    assert ctl._hold_power == 3100 and ctl._hold_since == rig.clock.t
    ctl._settle_hold(rig.clock.t + C.STABLE_RUN_S)  # a clean full run may witness
    assert ctl._proven_max == 3100


def test_the_proven_timestamp_refreshes_at_most_hourly(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    rig.coord.data["wattage_limit"] = 3000  # power in force == the hold
    ctl._proven_max, ctl._proven_max_at = 3000, rig.clock.t
    ctl._hold_power, ctl._hold_since = 3000, rig.clock.t - C.STABLE_RUN_S

    ctl._note_hold(True, rig.clock.t + C.STABLE_RUN_S)  # stable but < 1 h old
    assert ctl._proven_max_at == rig.clock.t
    ctl._note_hold(True, rig.clock.t + C.PROVEN_REFRESH_S)
    assert ctl._proven_max_at == rig.clock.t + C.PROVEN_REFRESH_S


# --------------------------------------------------------------- the ceiling


def test_armed_with_a_witness_caps_at_proven_plus_margin(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_fault(rig.clock.t, 4200, "error code 2320")
    # Witness AFTER the fault (a clean hold it has proven since) wins.
    ctl._proven_max, ctl._proven_max_at = 3000, rig.clock.t + 100
    assert ctl._bounds(rig.clock.t) == (1000, 3000 + MARGIN)
    faults = _faults(ctl, rig)
    assert faults["ceiling_w"] == 3250 and faults["proven_max_w"] == 3000
    assert faults["fault_power_w"] == 4200 and faults["fault_reason"] == "error code 2320"


def test_a_stale_witness_loses_to_the_fault_power(monkeypatch):
    # Seen before the fault: it says nothing about what the miner holds now.
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._proven_max, ctl._proven_max_at = 3000, rig.clock.t - 100
    ctl._note_fault(rig.clock.t, 1224, "error code 2320")
    assert ctl._bounds(rig.clock.t) == (1000, 1224 + MARGIN)


def test_armed_without_a_witness_caps_at_fault_plus_margin(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_fault(rig.clock.t, 4200, "error code 2320")
    assert ctl._proven_max is None
    assert ctl._bounds(rig.clock.t) == (1000, 4200 + MARGIN)


def test_armed_with_no_fault_power_uses_power_min_as_reference(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_fault(rig.clock.t, None, "error code 9999")
    assert ctl._bounds(rig.clock.t) == (1000, 1000 + MARGIN)


def test_the_cap_clamps_into_the_configured_bounds(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_fault(rig.clock.t, 100, "error code 2320")  # 100 + 250 sits below Power Min
    assert ctl._bounds(rig.clock.t) == (1000, 1000)
    ctl._note_fault(rig.clock.t, 4900, "error code 2320")  # 4900 + 250 above Power Max
    assert ctl._bounds(rig.clock.t) == (1000, 5000)


def test_evidence_expires_after_seven_days(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_crash(2000, rig.clock.t)
    ctl._note_fault(rig.clock.t, 3000, "error code 2320")
    assert ctl._bounds(rig.clock.t) == (2250, 3250)
    assert ctl._bounds(rig.clock.t + TTL - 60) == (2250, 3250)  # still armed
    assert ctl._bounds(rig.clock.t + TTL + 60) == (1000, 5000)  # disarmed


def test_on_inversion_the_ceiling_wins_and_logs_once(monkeypatch, caplog):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    caplog.set_level(logging.WARNING, logger="wm.controller")
    ctl._note_crash(3600, rig.clock.t)  # crash floor 3850...
    ctl._note_fault(rig.clock.t, 1224, "external derate to 1224W (commanded 2400W)")
    # ...over a 1474 W ceiling: the ceiling wins down to Power Min (protect the hardware).
    assert ctl._bounds(rig.clock.t) == (1474, 1474)
    assert ctl._cap_inversion_logged is True
    assert sum("Fault bounds inverted" in r.message for r in caplog.records) == 1
    ctl._bounds(rig.clock.t)
    assert sum("Fault bounds inverted" in r.message for r in caplog.records) == 1  # once


def test_commands_are_clamped_to_the_fault_ceiling(monkeypatch):
    # No plan option, no cap toggle in v2.0: every power command is clamped.
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    rig.coord.data["up_freq_finish"] = 1  # tuned, so v3 carries the command
    ctl._note_fault(rig.clock.t, 2500, "external derate to 2500W (commanded 4200W)")

    async def go():
        await ctl._command_power(4000)  # wants far more than the 2750 W cap

    run_async(go())
    assert rig.calls == [("set_miner_power", 2500 + MARGIN)]
    assert ctl._op_target == 2750  # and it never asks the miner past the cap


# ------------------------------------------------------- error codes (poll path)


def test_a_new_error_code_arms_the_ceiling_and_taints_the_hold(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    rig.coord.data["wattage_limit"] = 3000  # the power in force when it faulted
    ctl._hold_power, ctl._hold_since = 3000, rig.clock.t - 3600

    rig.coord.data["error_codes"] = {"2320": "2026-10-17 17:33:06"}
    ctl._note_error_codes(rig.clock.t)
    assert "whatsminer_miner_error_2320" in [k for k, _, _ in pn.created]
    assert ctl._fault_at == rig.clock.t
    assert ctl._fault_power == 3000  # the operating power in force
    assert ctl._fault_reason == "error code 2320"
    assert ctl._hold_tainted is True
    assert ctl._bounds(rig.clock.t) == (1000, 3250)

    ctl._note_error_codes(rig.clock.t)  # the same pair is not new evidence
    assert [k for k, _, _ in pn.created].count("whatsminer_miner_error_2320") == 1


# --------------------------------------------------------------- persistence


def test_fault_state_survives_a_restart(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved[KEY] = {
        "proven_max": 3000, "proven_max_at": T0 - 100.0,
        "fault_at": T0 - 50.0, "fault_power": 3000, "fault_reason": "error code 2320",
        "crash_floor": 2250, "crash_at": T0 - 40.0,
    }
    run_async(rig.ctl.async_setup())
    assert rig.ctl._proven_max == 3000 and rig.ctl._fault_power == 3000
    assert rig.ctl._bounds(rig.clock.t) == (2250, 3250)


def test_legacy_proven_fault_keys_are_honoured(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved[KEY] = {
        "proven_fault_at": T0 - 50.0, "proven_fault_power": 3000,
        "proven_fault_reason": "error code 2320",
    }
    run_async(rig.ctl.async_setup())
    assert rig.ctl._fault_power == 3000 and rig.ctl._fault_reason == "error code 2320"
    assert rig.ctl._bounds(rig.clock.t) == (1000, 3250)


def test_an_expired_fault_stays_disarmed_across_a_restart(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved[KEY] = {"fault_at": T0 - 8 * 86400.0, "fault_power": 3000}
    run_async(rig.ctl.async_setup())
    assert rig.ctl._fault_at is not None  # remembered, but no longer evidence
    assert rig.ctl._bounds(rig.clock.t) == (1000, 5000)


# ----------------------------------------------------------- crash floor

def test_a_crash_raises_the_floor_while_fresh(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_crash(None, rig.clock.t)  # unknown power: logged only, not evidence
    assert ctl._crash_floor is None
    ctl._note_crash(2000, rig.clock.t)
    assert ctl._crash_floor == 2250
    assert ctl._crash_at == rig.clock.t
    assert "whatsminer_crash_floor" in [k for k, _, _ in pn.created]
    assert _faults(ctl, rig)["floor_w"] == 2250
    ctl._note_crash(1200, rig.clock.t)  # a lower crash never lowers the floor
    assert ctl._crash_floor == 2250
    assert ctl._bounds(rig.clock.t + TTL - 60)[0] == 2250  # fresh inside the TTL
    assert ctl._bounds(rig.clock.t + TTL + 60)[0] == 1000  # expired


# ---------------------------------------------------------------- the reset


def test_reset_fault_memory_forgets_everything(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_crash(3600, rig.clock.t)
    ctl._note_fault(rig.clock.t, 1224, "external derate to 1224W (commanded 2400W)")
    ctl._proven_max, ctl._proven_max_at = 3000, rig.clock.t - 100
    ctl._external_derate = True
    ctl._derate_candidate_since, ctl._derate_candidate_polls = rig.clock.t - 60, 2
    ctl._derate_match_polls = 2
    ctl._error_notify_keys.add("miner_error_2320")
    # Armed: the witness (3000, pre-fault) LOSSES to the fault power (1224),
    # and the 3850 W crash floor inverts over the 1474 W ceiling — the
    # ceiling wins.
    assert ctl._bounds(rig.clock.t) == (1474, 1474)

    async def go():
        await ctl.async_reset_fault_memory()

    run_async(go())
    assert ctl._crash_at is None and ctl._crash_floor is None
    assert ctl._fault_at is None and ctl._fault_power is None and ctl._fault_reason is None
    assert ctl._proven_max is None and ctl._proven_max_at is None
    assert ctl._hold_tainted is False and ctl._external_derate is False
    assert ctl._derate_candidate_since is None and ctl._derate_candidate_polls == 0
    assert ctl._derate_match_polls == 0 and ctl._error_notify_keys == set()
    assert ctl._bounds(rig.clock.t) == (1000, 5000)
    faults = rig.pid_state["faults"]
    assert faults["floor_w"] == 1000 and faults["ceiling_w"] == 5000
    assert faults["fault_power_w"] is None and faults["proven_max_w"] is None
    for key in ("whatsminer_crash_floor", "whatsminer_miner_error_2320"):
        assert key in pn.dismissed
    saved = Store.saved[KEY]
    assert saved["fault_at"] is None and saved["crash_floor"] is None
    assert saved["proven_max"] is None


def test_crashes_inside_a_fault_episode_do_not_raise_the_floor(monkeypatch):
    """v2.0.3: crashes during an armed fault episode are the fault's story."""
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_fault(rig.clock.t, 1800, "error code 2320")
    reg = C._Regression(prev_uptime=1000, seen_uptime=10, ours=False, run_s=300)
    ctl._count_restart(reg, rig.clock.t)
    assert ctl._crash_floor is None
    rig.clock.t += TTL + 60.0  # episode expired
    ctl._count_restart(
        C._Regression(prev_uptime=1000, seen_uptime=10, ours=False, run_s=300), rig.clock.t
    )
    # power in force in this rig is the fake's 4200 W limit
    assert ctl._crash_floor == 4200 + C.CRASH_STEP_W


def test_a_witness_after_the_fault_beats_it_but_a_stale_one_does_not(monkeypatch):
    """v2.0.3 covered above; pin the boundary here on the equality edge."""
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    ctl._note_fault(rig.clock.t, 1224, "error code 2320")
    ctl._proven_max, ctl._proven_max_at = 3000, rig.clock.t  # same tick = not after
    assert ctl._bounds(rig.clock.t) == (1000, 1224 + MARGIN)
