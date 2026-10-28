"""Power regulation (v2.0): set.miner.power is the only power path.

Since 1.11 verified set.miner.power on this firmware (M64 20250409.15.REL,
API 3.0.1; /home/ethan/network/power-test-20261012-220429.log), power changes
are restart-free: 1250 W ramped 1884 -> 1244 W with no restart. The v2 limit
only carries the ceiling (raise once tuned, lower for reconciliation) and
safety clamps during the boot window. What was floor learning (episodes,
proven_ok) is now fault bounds (_bounds): a crash floor from uncommanded
short runs, a proven-hold ceiling while fault evidence is fresh
(docs/v2.0-simplification.md).

Everything here runs against fakes; nothing reaches a real miner.
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
    # First test module loaded: install the HA stand-ins. When already present
    # (sibling test modules imported earlier), installing again would replace
    # the stub module objects they are bound to.
    ha_stubs.install()

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "whatsminer"
if "wm" not in sys.modules:
    pkg = types.ModuleType("wm")
    pkg.__path__ = [str(PKG)]
    sys.modules["wm"] = pkg

from test_api_v3 import SALT, FakeMiner, _PollAPI, v3  # noqa: E402

controller_mod = importlib.import_module("wm.controller")
coordinator_mod = importlib.import_module("wm.coordinator")
const = importlib.import_module("wm.const")
ds = importlib.import_module("wm.demand_shutoff")
# Bind the stub objects the controller itself holds, whatever order the test
# modules were imported in.
pn = controller_mod.persistent_notification
Store = controller_mod.Store
C = controller_mod

T0 = 1_700_000_000.0
MIN = 60.0
THERMOSTATS = ["climate.den", "climate.great_room", "climate.back_bedroom", "climate.windowed_bedroom"]
KEY = "whatsminer.e1.controller"


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def base_config(**over):
    cfg = {
        const.CONF_EXTERNAL_TEMP_SENSOR: "sensor.supply",
        const.CONF_PID_WEATHER_ENTITY: "weather.home",
        const.CONF_PID_DEMAND_ENTITIES: THERMOSTATS,
        const.CONF_PID_DEMAND_SHUTOFF_MODE: "active",
        const.CONF_PID_TARGET_TEMP: 104.0,
        const.CONF_PID_MIN_ADJUST_INTERVAL: 0,
        const.CONF_PID_MIN_ADJUST_INTERVAL_INCREASE: 0,
    }
    cfg.update(over)
    return cfg


class Rig:
    """A controller plus the fakes around it (self-contained copy of the smoke harness)."""

    def __init__(self, monkeypatch, **cfg_over):
        Store.saved.clear()
        pn.created.clear()
        pn.dismissed.clear()
        sys.modules["homeassistant.helpers.event"].timers.clear()
        self.clock = Clock()
        monkeypatch.setattr(controller_mod, "time", self.clock)
        self.hass = ha_stubs.FakeHass()
        self.coord = ha_stubs.FakeCoordinator()
        self.pid_state = {"target": 104.0}
        entry = types.SimpleNamespace(entry_id="e1")
        self.ctl = controller_mod.WhatsminerController(
            self.hass, entry, self.coord, self.pid_state, base_config(**cfg_over)
        )
        self.set_supply(100.0)
        self.set_weather(55.0, forecast_temps=[55.0] * 24)
        self.set_thermostats("heating")

    # --- environment knobs ---------------------------------------------------
    def set_supply(self, value):
        if value is None:
            self.hass.states.items["sensor.supply"] = ha_stubs.FakeState("unavailable")
        else:
            self.hass.states.items["sensor.supply"] = ha_stubs.FakeState(
                str(value), {"unit_of_measurement": "°F"}, now=self.clock.t
            )

    def set_weather(self, temp, forecast_temps):
        self.hass.states.items["weather.home"] = ha_stubs.FakeState(
            "sunny", {"temperature": temp, "temperature_unit": "°F"}, now=self.clock.t
        )
        from datetime import datetime, timezone

        self.hass.services.forecast = [
            {
                "datetime": datetime.fromtimestamp(self.clock.t + 3600 * (i + 1), tz=timezone.utc).isoformat(),
                "temperature": t,
            }
            for i, t in enumerate(forecast_temps)
        ]
        self.ctl._forecast_cache_time = None  # force refetch

    def set_thermostats(self, action, mode="heat", cur=69.5, sp=70.0, age=10.0, ids=None):
        for eid in ids or THERMOSTATS:
            self.hass.states.items[eid] = ha_stubs.FakeState(
                mode,
                {"hvac_action": action, "current_temperature": cur, "temperature": sp},
                age_s=age,
                now=self.clock.t,
            )

    def mining(self, on: bool, limit=None):
        self.coord.data["is_mining"] = on
        if limit is not None:
            self.coord.data["wattage_limit"] = limit

    # --- driving -----------------------------------------------------------------
    async def setup(self):
        await self.ctl.async_setup()

    async def tick(self, advance_s=30.0):
        self.clock.t += advance_s
        for eid in THERMOSTATS:
            st = self.hass.states.items.get(eid)
            if st is not None and st.state != "unavailable":
                self.set_thermostats(st.attributes["hvac_action"], st.state, st.attributes["current_temperature"],
                                     st.attributes["temperature"], 10.0, [eid])
        self.ctl._handle_coordinator_update()
        await asyncio.gather(*self.hass.tasks)
        self.hass.tasks.clear()

    async def run(self, minutes, step_s=30.0):
        n = int(minutes * MIN / step_s)
        for _ in range(n):
            await self.tick(step_s)

    @property
    def calls(self):
        return self.coord.api.calls


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------- v3 client


def test_set_miner_power_sends_whole_watts_as_a_number_with_a_super_token():
    async def go():
        async with FakeMiner(locked=False) as miner:
            resp = await miner.api().v3.set_miner_power(1250)
            assert resp["code"] == 0
            assert miner.powers == [1250]
            (req,) = [r for r in miner.v3_requests if r["cmd"] == "set.miner.power"]
            assert req["param"] == 1250 and isinstance(req["param"], int)  # a number, not "1250"
            assert req["account"] == "super"
            assert req["token"] == v3.v3_token("set.miner.power", "super", SALT, req["ts"])

    run_async(go())


def test_set_miner_power_unlocks_on_no_write_permission_then_retries_once():
    async def go():
        async with FakeMiner(locked=True) as miner:
            resp = await miner.api().v3.set_miner_power(1900)
            assert resp["code"] == 0
            assert miner.unlock_attempts == 1
            assert len([r for r in miner.v3_requests if r["cmd"] == "set.miner.power"]) == 2
            assert miner.powers == [1900]

    run_async(go())


def test_set_miner_power_rejection_raises_with_its_code():
    async def go():
        async with FakeMiner(locked=False) as miner:
            miner.reject_power = -1
            with pytest.raises(v3.WhatsminerV3Error) as err:
                await miner.api().v3.set_miner_power(1900)
            assert err.value.code == -1
            assert miner.unlock_attempts == 0  # only -4 unlocks

    run_async(go())


def test_set_miner_power_still_closed_after_unlock_raises_minus_4():
    async def go():
        async with FakeMiner(admin_password="other") as miner:
            with pytest.raises(v3.WhatsminerV3Error) as err:
                await miner.api(admin_password="hunter2").v3.set_miner_power(1900)
            assert err.value.code == v3.CODE_NO_WRITE_PERMISSION
            assert miner.powers == []

    run_async(go())


@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_set_miner_power_refuses_non_watts_before_any_io(bad):
    client = v3.WhatsminerV3API("192.0.2.1")  # TEST-NET: never contacted
    with pytest.raises(v3.WhatsminerV3Error):
        run_async(client.set_miner_power(bad))


def test_get_miner_status_summary_reports_up_freq_finish():
    async def go():
        async with FakeMiner() as miner:
            client = miner.api().v3
            assert (await client.get_miner_status("summary"))["up-freq-finish"] == 1
            miner.up_freq_finish = 0
            assert (await client.get_miner_status())["up-freq-finish"] == 0
            assert all("token" not in r for r in miner.v3_requests)  # a read needs no auth

    run_async(go())


def test_power_api_tracks_the_path_of_the_last_power_change():
    async def go():
        async with FakeMiner(locked=False) as miner:
            api = miner.api()
            assert api.power_api == "unknown"
            await api.set_miner_power(1900)
            assert api.power_api == "v3"
            api.note_power_fallback("set.miner.power rejected")
            await api.set_power_limit(1900)
            assert api.power_api == "v2_fallback"
            await api.set_miner_power(1950)
            assert api.power_api == "v3" and api.power_fallback is None
            await api.set_power_limit(5000)
            assert api.power_api == "v2"

    run_async(go())


# ------------------------------------------------------------ coordinator


class _V3Reads:
    def __init__(self, value):
        self.value = value
        self.reads = 0

    async def get_miner_status(self, param="summary"):
        self.reads += 1
        if isinstance(self.value, Exception):
            raise self.value
        return {"up-freq-finish": self.value}


def _poll_with(value, poll_up_freq):
    coord = coordinator_mod.WhatsminerCoordinator(
        None, "127.0.0.1", "admin", 4028, 30, "m64", poll_up_freq=poll_up_freq,
    )
    coord.api = _PollAPI({"STATUS": "S", "Msg": {}})
    coord.api.v3 = _V3Reads(value)
    return coord, run_async(coord._async_update_data())


def test_poll_reads_up_freq_finish_once_per_poll():
    coord, data = _poll_with(1, poll_up_freq=True)
    assert data["up_freq_finish"] == 1 and coord.api.v3.reads == 1
    coord, data = _poll_with(1, poll_up_freq=False)
    assert data["up_freq_finish"] is None and coord.api.v3.reads == 0


@pytest.mark.parametrize("value", [v3.WhatsminerV3Error("refused"), "x", None])
def test_poll_survives_a_failed_up_freq_read_as_not_tuned(value):
    _, data = _poll_with(value, poll_up_freq=True)
    assert data["up_freq_finish"] is None
    assert data["mac"] == "aa_bb_cc_00_11_22"


def _setup_kwargs(options):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wm.__init__", pathlib.Path(coordinator_mod.__file__).with_name("__init__.py")
    )
    init_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_mod)
    seen = {}

    class Stop(Exception):
        pass

    def fake_coordinator(**kwargs):
        seen.update(kwargs)
        raise Stop

    init_mod.WhatsminerCoordinator = fake_coordinator
    hass = type("Hass", (), {"data": {}})()
    entry = type("Entry", (), {"data": {"host": "127.0.0.1"}, "options": options, "title": "m64", "entry_id": "e1"})()
    with pytest.raises(Stop):
        run_async(init_mod.async_setup_entry(hass, entry))
    return seen


def test_setup_always_polls_up_freq_finish():
    """v2.0: set.miner.power is the only power path, so the tuning read is unconditional."""
    assert _setup_kwargs({})["poll_up_freq"] is True
    # A leftover power_regulation option from an old entry changes nothing.
    assert _setup_kwargs({"power_regulation": "adjust_power_limit"})["poll_up_freq"] is True


# ------------------------------------------------------- controller harness


def v3_rig(monkeypatch, store=None, up=1, **over):
    rig = Rig(monkeypatch, **over)
    if store:
        Store.saved[KEY] = dict(store)
    rig.coord.data.update({"up_freq_finish": up, "wattage": 3000, "hashrate_5s": 90.0})
    return rig


def cmds(rig, name):
    return [w for c, w in rig.calls if c == name]


def tune(rig, up):
    rig.coord.data["up_freq_finish"] = up


async def retune(rig):
    """The retune a power write opens (each dynamic change re-opens frequency
    search): up-freq-finish drops, then finishes — a new tuned epoch, so the
    write gate's one-non-safety-write budget re-arms."""
    tune(rig, 0)
    await rig.tick()
    tune(rig, 1)
    await rig.tick()


async def restart(rig, limit, up=0):
    """An Elapsed regression and a start edge, as btminer restarting looks from the poll."""
    rig.mining(False, limit=0)
    rig.coord.data["uptime"] = 0
    await rig.tick()
    rig.mining(True, limit=limit)
    tune(rig, up)
    rig.coord.data["uptime"] = 30
    await rig.tick()


# ------------------------------------------------------------ v3 happy path


def test_tuned_change_goes_out_as_set_miner_power(monkeypatch):
    rig = v3_rig(monkeypatch)
    rig.set_supply(104.0)  # on target: only the seed correction goes out, then steady

    async def go():
        await rig.setup()
        await rig.tick()
        assert cmds(rig, "set_power_limit") == []
        (sent,) = cmds(rig, "set_miner_power")
        assert sent < 4200
        assert rig.ctl._pending_restart_at is None  # not a restart: not attributed as ours
        assert rig.ctl._op_target == sent and rig.ctl._op_in_force == sent
        assert rig.ctl._op_established is True
        reg = rig.pid_state["power_command"]
        # Right after our own write the poll can't prove tuning finished (grace).
        assert reg["tuned"] is False and reg["regulated_power_w"] == sent and reg["established"] is True
        assert reg["limit_w"] == 4200 and reg["v3_error"] is None
        assert reg["last_command"] == f"set.miner.power {sent} W (regulate)"
        assert rig.coord.api.power_api == "v3"
        assert Store.saved[KEY]["regulated_power_w"] == sent
        await rig.run(2)  # past V3_TUNE_GRACE_S
        assert rig.pid_state["power_command"]["tuned"] is True
        assert cmds(rig, "set_miner_power") == [sent]  # nothing further goes out

    run_async(go())


# -------------------------------------------------------- not-tuned hold


def test_no_step_while_tuning_then_the_held_power_goes_out_once(monkeypatch):
    rig = v3_rig(monkeypatch)
    rig.set_supply(110.0)

    async def go():
        await rig.setup()
        await rig.tick()
        first = cmds(rig, "set_miner_power")
        assert len(first) == 1
        tune(rig, 0)  # the write started a re-tune
        rig.set_supply(118.0)
        await rig.run(15)
        assert cmds(rig, "set_miner_power") == first  # nothing while up-freq-finish is 0
        held = rig.ctl._op_target
        assert held < first[0] and rig.ctl._op_established is False
        assert rig.pid_state["power_command"]["tuned"] is False
        assert rig.ctl._op_in_force == first[0]  # only the target moved
        tune(rig, 1)
        await rig.tick()
        assert cmds(rig, "set_miner_power") == first + [rig.ctl._op_target]
        assert cmds(rig, "set_power_limit") == []

    run_async(go())


def test_a_stale_tuned_reading_right_after_our_write_is_not_trusted(monkeypatch):
    rig = v3_rig(monkeypatch)
    rig.set_supply(110.0)

    async def go():
        await rig.setup()
        await rig.tick()
        n = len(cmds(rig, "set_miner_power"))
        rig.set_supply(118.0)  # up-freq-finish still reads 1 (the poll lags the drop)
        await rig.tick()
        await rig.tick()
        assert len(cmds(rig, "set_miner_power")) == n
        await rig.run(2)  # past V3_TUNE_GRACE_S
        assert len(cmds(rig, "set_miner_power")) == n  # the grace still holds it
        # The epoch's one non-safety write is spent; the next act follows the
        # retune the write opened.
        await retune(rig)
        assert len(cmds(rig, "set_miner_power")) == n + 1

    run_async(go())


def test_supply_cap_keeps_the_limit_clamp_while_tuning(monkeypatch):
    """Safety during the boot window uses adjust_power_limit: set.miner.power applies next boot."""
    rig = v3_rig(monkeypatch, up=0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert rig.calls == []  # the first command is held: the miner is tuning
        rig.set_supply(123.0)
        await rig.tick()
        assert cmds(rig, "set_power_limit") == [1000]  # set.miner.power would only apply next boot
        assert cmds(rig, "set_miner_power") == []
        assert rig.ctl._limit_w == 1000 and rig.ctl._op_in_force == 1000  # regulation layer reset

    run_async(go())


def test_supply_cap_uses_set_miner_power_once_tuned(monkeypatch):
    rig = v3_rig(monkeypatch)

    async def go():
        await rig.setup()
        rig.set_supply(123.0)
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [1000]
        assert cmds(rig, "set_power_limit") == []
        assert rig.pid_state["control_mode"] == "safety_cap"

    run_async(go())


def test_a_held_target_does_not_suppress_a_safety_clamp_to_the_same_power(monkeypatch):
    """The held power was never delivered, so it is not the reference."""
    rig = v3_rig(monkeypatch, up=0)

    async def go():
        await rig.setup()
        await rig.tick()
        await rig.ctl._command_power(1000)  # held: still tuning
        assert rig.calls == [] and rig.ctl._op_target == 1000
        assert rig.ctl._last_commanded_power != 1000
        rig.set_supply(123.0)
        await rig.tick()
        assert cmds(rig, "set_power_limit") == [1000]

    run_async(go())


# --------------------------------------------------------------- ceiling


def test_ceiling_is_raised_once_for_an_increase_and_the_wanted_power_follows_the_tune(monkeypatch):
    rig = v3_rig(monkeypatch)
    rig.mining(True, limit=1950)
    rig.coord.data["wattage"] = 1940
    rig.set_supply(101.0)  # within the fine band: no soft start, the PID wants a bit more

    async def go():
        await rig.setup()
        for _ in range(10):
            await rig.tick()
            if cmds(rig, "set_power_limit"):
                break
        assert cmds(rig, "set_power_limit") == [5000]  # Power Max, as the ceiling
        wanted = rig.ctl._op_target
        assert 1950 < wanted < 5000
        assert rig.ctl._op_established is False
        assert rig.ctl._op_in_force == 5000  # the restart discarded the operating power
        reg = rig.pid_state["power_command"]
        assert reg["limit_w"] == 5000 and reg["regulated_power_w"] == wanted
        assert reg["last_command"] == "adjust_power_limit 5000 W (ceiling)"
        before = len(cmds(rig, "set_miner_power"))
        await restart(rig, limit=5000, up=0)  # the ceiling's own restart; tunes toward 5000
        await rig.run(15)
        assert len(cmds(rig, "set_miner_power")) == before  # nothing reaches a tuning miner
        tune(rig, 1)
        held = rig.ctl._op_target
        await rig.tick()
        assert cmds(rig, "set_miner_power")[before:] == [held]  # what was wanted while it tuned
        await rig.run(20)
        assert cmds(rig, "set_power_limit") == [5000]  # once; everything after is set.miner.power

    run_async(go())


@pytest.mark.parametrize("why", ["soft_start", "hot", "idle", "caps", "backed_off", "held"])
def test_ceiling_is_not_raised_when_the_loop_cannot_take_a_boot_at_power_max(monkeypatch, why):
    rig = v3_rig(monkeypatch)
    rig.mining(True, limit=1950)

    async def go():
        await rig.setup()
        rig.calls.clear()
        now = rig.clock.t
        caps = frozenset()
        if why == "soft_start":
            rig.ctl._soft_start = True
        elif why == "hot":
            rig.ctl._supply_hot_at = now
        elif why == "idle":
            rig.ctl._demand_summary = ds.ALL_IDLE
        elif why == "caps":
            caps = frozenset({"chip"})
        elif why == "backed_off":
            rig.ctl._v3_retry_at = now + 60
        else:
            rig.ctl._ceiling_hold_until = now + 60
        assert rig.ctl._ceiling_ok(now, caps, 104.0) is not None
        await rig.ctl._command_power(2400, caps=caps, temp=104.0)
        assert rig.calls == [("set_power_limit", 2400)]  # the increase goes out as the limit itself

    run_async(go())


def test_a_safety_clamp_above_a_lower_limit_never_raises_the_ceiling(monkeypatch):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 1000, "regulated_power_w": 1000})
    rig.mining(True, limit=1000)

    async def go():
        await rig.setup()
        rig.calls.clear()
        assert rig.ctl._ceiling_ok(rig.clock.t, frozenset(), 104.0) is None
        await rig.ctl._command_power(1950, safety=True, temp=104.0)
        assert rig.calls == [("set_power_limit", 1950)]  # the clamp itself, not Power Max
        assert rig.ctl._op_target == 1950

    run_async(go())


def test_lowered_power_max_lowers_the_ceiling_once(monkeypatch):
    rig = v3_rig(
        monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2500},
        **{const.CONF_POWER_MAX: 3500},
    )
    rig.coord.data["wattage"] = 2500
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.run(5)
        assert cmds(rig, "set_power_limit") == [3500]
        assert rig.ctl._op_target == 2500  # still the wanted power, re-commanded after the tune

    run_async(go())


def test_an_armed_fault_ceiling_lowers_the_limit_once_tuned(monkeypatch):
    """Fault evidence caps what every boot may tune toward: lower the ceiling, once."""
    rig = v3_rig(monkeypatch)
    Store.saved[KEY] = {
        "power_limit_w": 5000, "regulated_power_w": 2200,
        "fault_at": rig.clock.t, "fault_power": 1224,
        "fault_reason": "external derate to 1224W (commanded 5000W)",
        "proven_max": 2200, "proven_max_at": rig.clock.t + 60,  # witnessed after the fault
    }
    rig.mining(True, limit=5000)
    rig.coord.data["wattage"] = 2200
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        floor, ceiling = rig.ctl._bounds(rig.clock.t)
        assert ceiling == 2200 + C.PROVEN_MARGIN_W  # the witnessed hold plus margin
        await rig.run(5)
        assert cmds(rig, "set_power_limit") == [ceiling]  # exactly one deliberate restart
        # That restart re-tunes; the wanted power follows once tuning finishes
        # (the write gate budgets one non-safety write per tuned epoch).
        await restart(rig, limit=ceiling, up=0)
        tune(rig, 1)
        await rig.tick()
        assert ("set_miner_power", 2200) in rig.calls  # the wanted power follows once tuned
        assert rig.ctl._op_target == 2200
        faults = rig.pid_state["faults"]
        assert faults["ceiling_w"] == ceiling and faults["proven_max_w"] == 2200

    run_async(go())


# ------------------------------------------------------ fallback and reset


def test_rejection_falls_back_to_adjust_power_limit_resets_and_retries_v3_later(monkeypatch):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 3000})
    rig.set_supply(104.0)
    rig.coord.api.power_code = -1
    attempts = []
    real = rig.coord.api.set_miner_power

    async def counting(watts):
        attempts.append(watts)
        return await real(watts)

    rig.coord.api.set_miner_power = counting

    async def go():
        await rig.setup()
        await rig.tick()  # re-establishing 3000 W is rejected
        assert attempts == [3000]
        assert cmds(rig, "set_power_limit") == [3000]  # that command, via the limit
        assert rig.ctl._limit_w == 3000 and rig.ctl._op_in_force == 3000  # the ceiling is gone
        assert rig.ctl._op_established is True
        assert "whatsminer_power_fallback" in [k for k, _, _ in pn.created]
        assert rig.coord.api.power_api == "v2_fallback"
        assert rig.ctl._v3_retry_at == rig.clock.t + C.V3_POWER_RETRY_S  # 30 min backoff
        reg = rig.pid_state["power_command"]
        assert "code -1" in reg["v3_error"] and reg["v3_retry_at"]
        assert reg["last_command"] == "adjust_power_limit 3000 W (fallback)"
        # The fallback write opened a new tuned epoch (reset directly — this
        # test drives _command_power itself, no control step in between).
        rig.ctl._epoch_write_used = False
        await rig.ctl._command_power(2800)  # inside the retry window: straight to the limit
        assert attempts == [3000] and cmds(rig, "set_power_limit") == [3000, 2800]
        rig.clock.t += C.V3_POWER_RETRY_S
        rig.coord.api.power_code = None
        rig.ctl._epoch_write_used = False  # the 2800 W limit write's restart: new epoch
        await rig.ctl._command_power(2600)
        assert attempts == [3000, 2600] and cmds(rig, "set_miner_power") == [2600]
        assert "whatsminer_power_fallback" in pn.dismissed
        assert rig.coord.api.power_api == "v3"
        assert rig.ctl._v3_error is None

    run_async(go())


def test_transport_failures_fall_back_after_three_in_a_row(monkeypatch):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 3000})
    rig.set_supply(104.0)
    rig.coord.api.fail.add("set_miner_power")

    async def go():
        await rig.setup()
        await rig.ctl._command_power(2800)
        await rig.ctl._command_power(2800)
        assert rig.calls == []  # retried, not yet a restart
        await rig.ctl._command_power(2800)
        assert rig.calls == [("set_power_limit", 2800)]
        assert rig.ctl._v3_retry_at > rig.clock.t
        assert "whatsminer_power_fallback" in [k for k, _, _ in pn.created]

    run_async(go())


def test_after_any_limit_clamp_the_next_increase_raises_the_ceiling_again(monkeypatch):
    rig = v3_rig(monkeypatch, up=0, store={"power_limit_w": 5000, "regulated_power_w": 2500})
    rig.coord.data["wattage"] = 2500

    async def go():
        await rig.setup()
        rig.set_supply(123.0)
        await rig.tick()
        assert cmds(rig, "set_power_limit") == [1000]  # boot-window cap: the limit clamp
        rig.set_supply(104.0)  # the cap condition clears
        tune(rig, 1)
        rig.ctl._epoch_write_used = False  # the retune the cap's restart opened: new epoch
        rig.ctl._supply_hot_at = None
        rig.ctl._caps_active = frozenset()
        await rig.ctl._command_power(2400, temp=104.0)
        assert cmds(rig, "set_power_limit") == [1000, 5000]
        assert rig.ctl._op_target == 2400

    run_async(go())


# ------------------------------------------------ re-establish after starts


def test_every_start_edge_re_commands_the_regulated_power_once_tuned(monkeypatch):
    """Idempotent: right whether the firmware kept the power (3.0.3+) or reset it (3.0.1)."""
    rig = v3_rig(monkeypatch, up=0, store={"power_limit_w": 5000, "regulated_power_w": 2500})
    rig.mining(True, limit=5000)
    rig.coord.data["wattage"] = 2500
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.run(5)
        assert rig.calls == []  # HA restarted: held until the miner is tuned
        tune(rig, 1)
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [2500]
        await rig.run(10)
        assert cmds(rig, "set_miner_power") == [2500]  # once per start, not per tick
        await restart(rig, limit=5000, up=0)  # btminer restarted on its own
        assert rig.ctl._op_established is False and rig.ctl._op_in_force == 5000
        await rig.run(15)
        assert cmds(rig, "set_miner_power") == [2500]
        tune(rig, 1)
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [2500, 2500]
        assert cmds(rig, "set_power_limit") == []

    run_async(go())


def test_re_establishing_at_or_above_the_limit_sends_nothing(monkeypatch):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 3000, "regulated_power_w": 3000})
    rig.mining(True, limit=3000)
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert rig.calls == [] and rig.ctl._op_established is True

    run_async(go())


def test_a_transient_derated_limit_is_never_adopted(monkeypatch):
    """2026-10-12: 1905 W reported ~40 s after an uncommanded restart must not stick."""
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2000})
    rig.mining(True, limit=5000)
    rig.coord.data["wattage"] = 2000
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [2000]
        rig.mining(False, limit=0)
        rig.coord.data["uptime"] = 0
        await rig.tick()
        for up in (34, 64):  # up, not hashing yet, reporting the derate
            rig.mining(False, limit=1905)
            rig.coord.data["uptime"] = up
            tune(rig, 0)
            await rig.tick()
        for up in (94, 124, 154):
            rig.mining(True, limit=1905)
            rig.coord.data["uptime"] = up
            await rig.tick()
        assert rig.ctl._limit_w == 5000  # neither adopted nor answered
        assert rig.ctl._external_derate is False
        assert cmds(rig, "set_power_limit") == []

    run_async(go())


def test_reported_limit_is_adopted_only_after_it_persists(monkeypatch):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2000})
    rig.mining(True, limit=1905)
    rig.coord.data["uptime"] = 60
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()  # uptime 60: boot transient
        await rig.tick()
        assert rig.ctl._limit_w == 5000
        for up in (130, 160):
            rig.coord.data["uptime"] = up
            await rig.tick()
        assert rig.ctl._limit_w == 5000
        rig.coord.data["uptime"] = 190
        await rig.tick()
        assert rig.ctl._limit_w == 1905

    run_async(go())


def test_a_reported_limit_equal_to_the_regulated_power_is_not_adopted(monkeypatch):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2000})
    rig.mining(True, limit=5000)
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [2000]
        rig.mining(True, limit=2000)  # as if the v2 field mirrored the operating power
        await rig.run(5)
        assert rig.ctl._limit_w == 5000

    run_async(go())


# ------------------------------------------------------------- crash floor

# Replaces floor learning (1.12-1.14): an uncommanded short run now raises the
# fault floor plus CRASH_STEP_W for FAULT_TTL_S, and a full stable run
# witnesses a proven hold. One bounds source, in _bounds().


async def crash_at(rig, minutes=5):
    """Hash for a few minutes (tuned), then an uncommanded restart confirmed by the next poll."""
    for _ in range(int(minutes * 2)):
        rig.coord.data["uptime"] += 30
        await rig.tick()
    tune(rig, 0)  # a real btminer restart re-tunes: nothing is delivered mid-restart
    rig.coord.data["uptime"] = 10
    await rig.tick()
    rig.coord.data["uptime"] = 40
    await rig.tick()


def test_an_uncommanded_short_run_raises_the_crash_floor_for_a_week(monkeypatch):
    rig = v3_rig(
        monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 1700},
        **{const.CONF_POWER_MIN: 1200},
    )
    rig.mining(True, limit=5000)
    rig.coord.data.update({"uptime": 60, "wattage": 1700})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [1700]
        assert rig.ctl._crash_floor is None
        await crash_at(rig)  # a five-minute run, then btminer restarts on its own
        assert rig.ctl._crash_floor == 1700 + C.CRASH_STEP_W
        floor_at = rig.clock.t
        assert rig.ctl._bounds(floor_at)[0] == 1950  # crash power + 250 W
        faults = rig.pid_state["faults"]
        assert faults["crash_floor_w"] == 1950 and faults["floor_w"] == 1950
        assert faults["crash_at"] is not None
        assert "whatsminer_crash_floor" in [k for k, _, _ in pn.created]
        assert Store.saved[KEY]["crash_floor"] == 1950
        await rig.run(2)  # still re-tuning: nothing goes out
        assert cmds(rig, "set_miner_power") == [1700]
        tune(rig, 1)
        await rig.tick()  # tuned: the floor clamps the re-establish, no restart involved
        assert cmds(rig, "set_miner_power") == [1700, 1950]
        assert cmds(rig, "set_power_limit") == []
        rig.clock.t += C.FAULT_TTL_S  # seven days later the evidence is stale
        assert rig.ctl._bounds(rig.clock.t)[0] == 1200  # Power Min again

    run_async(go())


def test_the_crash_floor_is_clamped_at_power_max(monkeypatch):
    rig = v3_rig(monkeypatch, **{const.CONF_POWER_MAX: 5000})

    async def go():
        await rig.setup()
        rig.ctl._note_crash(4900, rig.clock.t)
        assert rig.ctl._crash_floor == 5150  # power + step is allowed to exceed names…
        floor, ceiling = rig.ctl._bounds(rig.clock.t)
        assert floor == 5000  # …but the bound itself clamps at Power Max
        assert ceiling == 5000
        rig.ctl._note_crash(4000, rig.clock.t)  # still fresh: only ever ratchets up
        assert rig.ctl._crash_floor == 5150

    run_async(go())


def test_a_stable_run_witnesses_a_proven_hold(monkeypatch):
    """900 s+ of clean hashing at a power proves it: _proven_max; no bound moves while unarmed."""
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2200})
    rig.mining(True, limit=5000)
    rig.coord.data.update({"uptime": 5000, "wattage": 2200})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()  # re-commands 2200 W: in force from now
        assert cmds(rig, "set_miner_power") == [2200]
        t0 = rig.clock.t
        while rig.clock.t < t0 + C.STABLE_RUN_S + 60:
            rig.coord.data["uptime"] += 30
            await rig.tick()
        assert rig.ctl._proven_max is None  # a hold still in progress proves nothing
        rig.coord.data["uptime"] = 10  # the run ends (uncommanded) after well over 15 min
        await rig.tick()
        assert rig.ctl._proven_max == 2200  # witnessed as the segment closed
        assert rig.ctl._proven_max_at == rig.clock.t
        rig.coord.data["uptime"] = 40
        await rig.tick()
        assert rig.ctl._crash_floor is None  # a long run is no crash even uncommanded
        floor, ceiling = rig.ctl._bounds(rig.clock.t)
        assert floor == 1000 and ceiling == 5000  # fault unarmed: nothing raised
        assert "whatsminer_crash_floor" not in [k for k, _, _ in pn.created]

    run_async(go())


def test_reset_fault_memory_reopens_the_bounds(monkeypatch):
    rig = v3_rig(monkeypatch, **{const.CONF_POWER_MIN: 1200})

    async def go():
        await rig.setup()
        rig.ctl._note_crash(1400, rig.clock.t)  # floor 1650
        rig.ctl._note_fault(rig.clock.t, 3800, "external derate to 3800W")  # ceiling 4050
        assert rig.ctl._bounds(rig.clock.t) == (1650, 4050)
        await rig.ctl.async_reset_fault_memory()
        assert rig.ctl._bounds(rig.clock.t) == (1200, 5000)
        assert rig.ctl._crash_floor is None and rig.ctl._fault_at is None
        assert rig.ctl._proven_max is None
        faults = rig.pid_state["faults"]
        assert faults["floor_w"] == 1200 and faults["ceiling_w"] == 5000
        assert "whatsminer_crash_floor" in pn.dismissed

    run_async(go())


def test_on_inverted_bounds_the_ceiling_wins(monkeypatch):
    """Fault floor above the fault ceiling: protect the hardware, clamp everything low."""
    rig = v3_rig(monkeypatch, **{const.CONF_POWER_MIN: 1200})

    async def go():
        await rig.setup()
        rig.ctl._note_crash(1700, rig.clock.t)  # floor would be 1950
        rig.ctl._note_fault(rig.clock.t, 1224, "error code 2320")  # ceiling 1474 < 1950
        assert rig.ctl._bounds(rig.clock.t) == (1474, 1474)  # the ceiling wins
        assert rig.ctl._cap_inversion_logged is True
        rig.ctl._crash_at = None  # floor evidence expires…
        rig.ctl._crash_floor = None
        assert rig.ctl._bounds(rig.clock.t) == (1200, 1474)  # …the floor drops back to Power Min
        assert rig.ctl._cap_inversion_logged is False

    run_async(go())


# ------------------------------------------------------ hashrate collapse


def test_hashrate_collapse_after_a_reduction_is_observed_not_undone(monkeypatch):
    """v2.1: the collapse is reported (log, notification, regulation error), and the
    crash floor still arms — but no corrective adjust_power_limit answers a documented
    side effect of one dynamic write with a certain restart."""
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2500})
    rig.mining(True, limit=5000)
    rig.coord.data.update({"wattage": 2500, "hashrate_5s": 100.0})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [2500]
        await rig.run(2)  # past V3_TUNE_GRACE_S
        await retune(rig)  # the 2500 W write opened a tune; it finished: new epoch
        await rig.ctl._command_power(1800)
        assert cmds(rig, "set_miner_power") == [2500, 1800]
        tune(rig, 0)
        rig.coord.data.update({"wattage": 1800, "hashrate_5s": 60.0})  # tracks: 33 vs 40 TH/kW
        await rig.run(3)
        assert cmds(rig, "set_power_limit") == []
        rig.coord.data.update({"hashrate_5s": 10.0})  # 5.6 TH/kW: collapsed
        await rig.run(3)
        assert cmds(rig, "set_power_limit") == []  # observed, never undone
        assert cmds(rig, "set_miner_power") == [2500, 1800]  # no churn either
        assert rig.ctl._crash_floor == 2050  # crash evidence at the failed 1800 W + 250 W
        assert rig.ctl._bounds(rig.clock.t)[0] == 2050
        assert "whatsminer_power_collapse" in [k for k, _, _ in pn.created]
        assert "whatsminer_crash_floor" in [k for k, _, _ in pn.created]
        assert rig.pid_state["faults"]["floor_w"] == 2050
        assert Store.saved[KEY]["crash_floor"] == 2050
        # The regulation error is published and self-clears on a stable read
        assert rig.pid_state["power_command"]["regulation_error"] == "collapse-observed-no-undo"
        rig.coord.data.update({"hashrate_5s": 80.0})  # 44 TH/kW: recovered
        await rig.tick()
        assert rig.pid_state["power_command"]["regulation_error"] is None
        await rig.ctl.async_reset_fault_memory()
        assert rig.ctl._crash_floor is None and rig.ctl._bounds(rig.clock.t)[0] == 1000
        assert "whatsminer_power_collapse" in pn.dismissed

    run_async(go())


# -------------------------------------------------------- tuning timeout


def test_a_miner_that_never_finishes_tuning_is_left_alone_until_it_does(monkeypatch):
    """v2.1: the tuning-timeout limit write is itself gated on up-freq-finish —
    the firmware owns the tune; the wanted power is delivered (v3, restart-free)
    once tuning finishes."""
    rig = v3_rig(monkeypatch, up=0, store={"power_limit_w": 5000, "regulated_power_w": 2000})
    rig.mining(True, limit=5000)
    rig.coord.data.update({"wattage": 3400})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.run(C.V3_TUNE_TIMEOUT_S / 60 - 1)
        assert rig.calls == []
        await rig.run(2)  # past the tuning timeout
        assert rig.calls == []  # gated: nothing reaches a miner that never tunes
        tune(rig, 1)
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [2000]  # the wanted power, once tuned
        await rig.run(60)
        assert rig.calls == [("set_miner_power", 2000)]  # once

    run_async(go())


# -------------------------------------------------------- set-and-hold (pinned)


def test_a_collapsed_band_pins_the_target_and_reconciles_once(monkeypatch):
    """power_max - power_min below the fine step: the PID is bypassed for a
    fixed Power-Min target, delivered once through the tuned v3 path."""
    rig = v3_rig(monkeypatch, **{const.CONF_POWER_MIN: 1300, const.CONF_POWER_MAX: 1325})
    rig.mining(True, limit=1325)  # at the band ceiling: 25 W above the pinned target
    rig.set_supply(100.0)  # cold supply: a PID would push to the ceiling every step

    async def go():
        await rig.setup()
        assert rig.pid_state["pid_mode"] == "pinned"
        await rig.tick()
        assert rig.calls == []  # first tick only learns: nothing commanded yet
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [1300]  # the pinned target, restart-free
        assert rig.ctl._op_target == 1300 and rig.ctl._op_in_force == 1300
        await rig.run(30)
        assert cmds(rig, "set_miner_power") == [1300]  # held: no PID churn inside the band
        assert rig.ctl._op_established is True
        assert rig.pid_state["power_command"]["regulated_power_w"] == 1300

    run_async(go())


def test_pinned_mode_gets_one_reconcile_write_per_tuned_epoch(monkeypatch):
    """The reconcile goes through the write gate: one non-safety write per
    tuned epoch — the next one waits for the retune the first one opened."""
    rig = v3_rig(monkeypatch, **{const.CONF_POWER_MIN: 1300, const.CONF_POWER_MAX: 1325})
    rig.mining(True, limit=1325)
    rig.set_supply(100.0)

    async def go():
        await rig.setup()
        await rig.tick()
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [1300]  # the epoch's one write
        # Anything else non-safety is held, not sent — the budget is spent.
        await rig.ctl._command_power(1310)
        assert cmds(rig, "set_miner_power") == [1300]
        assert rig.ctl._op_target == 1310  # held as the target, not lost
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [1300]  # the tick cannot buy a second write
        await retune(rig)  # the tune our write opened finishes: a new epoch
        await rig.ctl._command_power(1310)
        assert cmds(rig, "set_miner_power") == [1300, 1310]  # delivered once the gate opened

    run_async(go())


def test_pinned_mode_keeps_the_safety_clamp_authority(monkeypatch):
    """Set-and-hold bypasses the PID, not the safety paths: with the gate
    closed (untuned) and the band pinned, a chip cap still forces Power Min
    through adjust_power_limit."""
    rig = v3_rig(monkeypatch, up=0, **{const.CONF_POWER_MIN: 1300, const.CONF_POWER_MAX: 1325})
    rig.mining(True, limit=2000)  # believed far above the cap clamp: enough step to fire
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert rig.calls == []  # pinned + untuned: the gate holds everything
        rig.coord.data["temperature_avg"] = 190.0  # chip cap engages (cap 185 °F)
        await rig.tick()
        assert cmds(rig, "set_miner_power") == []
        assert cmds(rig, "set_power_limit") == [1300]  # the cap clamp: Power Min, ungated
        assert rig.pid_state["power_command"]["regulation_error"] is None  # not a regulation error

    run_async(go())


# ------------------------------------------------------------- the write gate


def test_the_gate_holds_non_safety_writes_while_tuning_but_not_safety_clamps(monkeypatch):
    """up-freq-finish != 1 means the tuner owns the miner: a non-safety raise
    waits; a safety clamp (chip cap) still flies."""
    rig = v3_rig(monkeypatch, up=0)
    rig.mining(True, limit=3000)
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert rig.calls == []  # the seed command is gated too
        await rig.ctl._command_power(2800)  # non-safety: held, not sent
        assert cmds(rig, "set_miner_power") == [] and cmds(rig, "set_power_limit") == []
        assert rig.ctl._op_target == 2800  # held as the target, not lost
        rig.coord.data["temperature_avg"] = 190.0  # chip cap engages (cap 185 °F)
        await rig.tick()
        assert cmds(rig, "set_power_limit") == [1000]  # the safety clamp: below every gate

    run_async(go())
