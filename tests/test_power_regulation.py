"""Power Regulation set_miner_power (1.11): v3 set.miner.power instead of restarts.

Live evidence (2026-10-12, M64 20250409.15.REL, API 3.0.1;
/home/ethan/network/power-test-20261012-220429.log): set.miner.power 1250 was
accepted and ramped 1884 -> 1244 W with no restart; up-freq-finish dropped to 0
for the re-tune; v2 adjust_power_limit afterwards discarded the operating
power; a derated 1905 W telemetry limit ~40 s after an uncommanded restart made
floor enforcement send adjust_power_limit(1950), a restart mid-regulation.

Everything here runs against fakes; nothing reaches a real miner.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_api_v3 import SALT, FakeMiner, _PollAPI, v3  # noqa: E402
from test_controller_smoke import MIN, Rig, Store, const, controller_mod, pn, run_async  # noqa: E402

coordinator_mod = importlib.import_module("wm.coordinator")
C = controller_mod
V3 = {const.CONF_POWER_REGULATION: const.POWER_REGULATION_V3}
KEY = "whatsminer.e1.controller"


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


def test_poll_reads_up_freq_finish_only_with_set_miner_power():
    coord, data = _poll_with(1, poll_up_freq=True)
    assert data["up_freq_finish"] == 1 and coord.api.v3.reads == 1
    coord, data = _poll_with(1, poll_up_freq=False)
    assert data["up_freq_finish"] is None and coord.api.v3.reads == 0  # legacy polls as before


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


def test_setup_polls_up_freq_only_when_set_miner_power_is_selected():
    assert _setup_kwargs({})["poll_up_freq"] is False
    assert _setup_kwargs({"power_regulation": "adjust_power_limit"})["poll_up_freq"] is False
    assert _setup_kwargs({"power_regulation": "set_miner_power"})["poll_up_freq"] is True


def test_option_strings_match_in_strings_and_translations():
    root = pathlib.Path(coordinator_mod.__file__).parent
    texts = [json.loads((root / p).read_text(encoding="utf-8")) for p in ("strings.json", "translations/en.json")]
    picks = [
        (t["options"]["step"]["init"]["data"]["power_regulation"],
         t["options"]["step"]["init"]["data_description"]["power_regulation"])
        for t in texts
    ]
    assert picks[0] == picks[1]
    assert "adjust_power_limit" in picks[0][1] and "set_miner_power" in picks[0][1]
    assert const.DEFAULT_POWER_REGULATION == "adjust_power_limit"
    assert const.POWER_REGULATION_MODES == ["adjust_power_limit", "set_miner_power"]


# ------------------------------------------------------------ controller


def v3_rig(monkeypatch, store=None, up=1, **over):
    rig = Rig(monkeypatch, **{**V3, **over})
    if store:
        Store.saved[KEY] = dict(store)
    rig.coord.data.update({"up_freq_finish": up, "wattage": 3000, "hashrate_5s": 90.0})
    return rig


def cmds(rig, name):
    return [w for c, w in rig.calls if c == name]


def tune(rig, up):
    rig.coord.data["up_freq_finish"] = up


async def restart(rig, limit, up=0):
    """An Elapsed regression and a start edge, as btminer restarting looks from the poll."""
    rig.mining(False, limit=0)
    rig.coord.data["uptime"] = 0
    await rig.tick()
    rig.mining(True, limit=limit)
    tune(rig, up)
    rig.coord.data["uptime"] = 30
    await rig.tick()


def test_legacy_default_never_sends_set_miner_power(monkeypatch):
    rig = Rig(monkeypatch)
    rig.coord.data["up_freq_finish"] = 1

    async def go():
        await rig.setup()
        rig.set_supply(110.0)
        await rig.run(10)
        assert cmds(rig, "set_power_limit")
        assert cmds(rig, "set_miner_power") == []
        reg = rig.pid_state["power_regulation"]
        assert reg["mode"] == "adjust_power_limit" and reg["tuned"] is None

    run_async(go())


def test_tuned_change_within_the_limit_goes_out_as_set_miner_power(monkeypatch):
    rig = v3_rig(monkeypatch)
    rig.set_supply(110.0)  # above target: the PID wants less than the 4200 W limit

    async def go():
        await rig.setup()
        await rig.tick()
        assert cmds(rig, "set_power_limit") == []
        (sent,) = cmds(rig, "set_miner_power")
        assert sent < 4200
        assert rig.ctl._pending_restart_at is None  # not a restart: not attributed as ours
        reg = rig.pid_state["power_regulation"]
        assert reg["path"] == "v3" and reg["regulated_power_w"] == sent and reg["established"] is True
        assert Store.saved[KEY]["regulated_power_w"] == sent

    run_async(go())


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
        assert rig.pid_state["power_regulation"]["tuned"] is False
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
        assert len(cmds(rig, "set_miner_power")) == n + 1

    run_async(go())


def test_supply_cap_keeps_the_limit_clamp_while_tuning(monkeypatch):
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
        assert rig.pid_state["power_regulation"]["ceiling_in_force"] is True
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
            rig.ctl._demand_summary = C.ds.ALL_IDLE
        elif why == "caps":
            caps = frozenset({"chip"})
        elif why == "backed_off":
            rig.ctl._v3_retry_at = now + 60
        else:
            rig.ctl._ceiling_hold_until = now + 60
        assert rig.ctl._ceiling_ok(now, caps, 104.0) is not None
        await rig.ctl._command_power(2400, caps=caps, temp=104.0)
        assert rig.calls == [("set_power_limit", 2400)]  # the increase, as legacy sends it

    run_async(go())



@pytest.mark.parametrize("kind", ["safety", "floor_fire"])
def test_a_clamp_or_floor_enforcement_above_a_lower_limit_never_raises_the_ceiling(monkeypatch, kind):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 1000, "regulated_power_w": 1000})
    rig.mining(True, limit=1000)

    async def go():
        await rig.setup()
        rig.calls.clear()
        assert rig.ctl._ceiling_ok(rig.clock.t, frozenset(), 104.0) is None
        await rig.ctl._command_power(1950, **{kind: True}, temp=104.0)
        assert rig.calls == [("set_power_limit", 1950)]

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
        reg = rig.pid_state["power_regulation"]
        assert reg["path"] == "v2_fallback" and "code -1" in reg["v3_error"] and reg["v3_retry_at"]
        await rig.ctl._command_power(2800)  # inside the retry window: straight to the limit
        assert attempts == [3000] and cmds(rig, "set_power_limit") == [3000, 2800]
        rig.clock.t += C.V3_POWER_RETRY_S
        rig.coord.api.power_code = None
        await rig.ctl._command_power(2600)
        assert attempts == [3000, 2600] and cmds(rig, "set_miner_power") == [2600]
        assert "whatsminer_power_fallback" in pn.dismissed
        assert rig.coord.api.power_api == "v3"

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

    run_async(go())


def test_after_any_limit_clamp_the_next_increase_raises_the_ceiling_again(monkeypatch):
    rig = v3_rig(monkeypatch, up=0, store={"power_limit_w": 5000, "regulated_power_w": 2500})
    rig.coord.data["wattage"] = 2500

    async def go():
        await rig.setup()
        rig.set_supply(123.0)
        await rig.tick()
        assert cmds(rig, "set_power_limit") == [1000]  # boot-window cap: the limit clamp
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


def test_a_transient_derated_limit_does_not_trigger_floor_enforcement(monkeypatch):
    """2026-10-12: 1905 W reported ~40 s after an uncommanded restart, floor 1950 W."""
    rig = v3_rig(
        monkeypatch,
        store={"power_limit_w": 5000, "regulated_power_w": 2000, "floor_learned": 1950},
        **{const.CONF_POWER_MIN: 1200},
    )
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
        assert cmds(rig, "set_power_limit") == []  # no adjust_power_limit(1950) stomp
        assert rig.ctl._limit_w == 5000

    run_async(go())


def test_unknown_limit_enforces_the_floor_on_a_booting_miner_only_once_it_persists(monkeypatch):
    rig = v3_rig(monkeypatch, store={"floor_learned": 1950}, **{const.CONF_POWER_MIN: 1200})
    rig.mining(False, limit=1700)
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        for up in (20, 50, 80, 110):  # inside the boot transient
            rig.coord.data["uptime"] = up
            await rig.tick()
        assert rig.calls == []
        for up in (140, 170):
            rig.coord.data["uptime"] = up
            await rig.tick()
        assert cmds(rig, "set_power_limit") == [1950]

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


# ------------------------------------------------------- the floor learner


async def crash_at(rig, minutes=5):
    """Hash for a few minutes (tuned), then an uncommanded restart confirmed by the next poll."""
    for _ in range(int(minutes * 2)):
        rig.coord.data["uptime"] += 30
        await rig.tick()
    rig.coord.data["uptime"] = 10
    await rig.tick()
    rig.coord.data["uptime"] = 40
    await rig.tick()


def test_short_runs_are_floor_evidence_at_the_operating_power_not_the_ceiling(monkeypatch):
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
        await crash_at(rig)
        assert rig.ctl._floor_short_runs == 1 and rig.ctl._floor_short_run_limit == 1700
        await rig.run(2)  # tuned again: 1700 is re-commanded
        assert cmds(rig, "set_miner_power") == [1700, 1700]
        await crash_at(rig)
        floor = rig.pid_state["power_floor"]
        assert floor["learned"] == 1950 and floor["unholdable_limit"] == 1700
        await rig.run(3)
        assert cmds(rig, "set_miner_power")[-1] == 1950  # floor enforced without a restart
        assert cmds(rig, "set_power_limit") == []

    run_async(go())


def test_restarts_at_a_power_already_held_are_a_fault_not_floor_evidence(monkeypatch):
    rig = v3_rig(monkeypatch, **{const.CONF_POWER_MIN: 1200})
    Store.saved[KEY] = {
        "power_limit_w": 5000, "regulated_power_w": 1950,
        "floor_proven_ok": 1899, "floor_proven_ok_at": rig.clock.t,  # held 1899 W recently
    }
    rig.mining(True, limit=5000)
    rig.coord.data.update({"uptime": 60, "wattage": 1950})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        await crash_at(rig)
        await rig.run(2)
        await crash_at(rig)
        floor = rig.pid_state["power_floor"]
        assert floor["learned"] is None and floor["fault_short_runs"] == 2
        assert "whatsminer_hardware_fault" in [k for k, _, _ in pn.created]

    run_async(go())


def test_a_power_is_proven_only_after_it_has_been_in_force_for_a_stable_run(monkeypatch):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2200})
    rig.mining(True, limit=5000)
    rig.coord.data.update({"uptime": 5000, "wattage": 2200})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()  # re-commands 2200 W: in force from now
        assert cmds(rig, "set_miner_power") == [2200]
        t0 = rig.clock.t
        while rig.clock.t + 30 < t0 + C.FLOOR_STABLE_S:
            rig.coord.data["uptime"] += 30
            await rig.tick()
        assert rig.ctl._floor_proven_ok is None  # the long Elapsed before it proves nothing
        for _ in range(2):
            rig.coord.data["uptime"] += 30
            await rig.tick()
        assert rig.ctl._floor_proven_ok == 2200

    run_async(go())


# ------------------------------------------------------ hashrate collapse


def test_hashrate_collapse_after_a_reduction_is_undone_and_bounds_regulation(monkeypatch):
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2500})
    rig.mining(True, limit=5000)
    rig.coord.data.update({"wattage": 2500, "hashrate_5s": 100.0})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [2500]
        await rig.run(2)
        await rig.ctl._command_power(1800)
        assert cmds(rig, "set_miner_power") == [2500, 1800]
        tune(rig, 0)
        rig.coord.data.update({"wattage": 1800, "hashrate_5s": 60.0})  # tracks: 33 vs 40 TH/kW
        await rig.run(3)
        assert cmds(rig, "set_power_limit") == []
        rig.coord.data.update({"hashrate_5s": 10.0})  # 5.6 TH/kW: collapsed
        await rig.run(3)
        assert cmds(rig, "set_power_limit") == [2500]
        assert rig.ctl._regulation_min_w == 2500 and rig.ctl._floor() == 2500
        assert "whatsminer_power_collapse" in [k for k, _, _ in pn.created]
        assert Store.saved[KEY]["regulation_min_w"] == 2500
        await rig.ctl.async_reset_learned_floor()
        assert rig.ctl._regulation_min_w is None and rig.ctl._floor() == 1000
        assert "whatsminer_power_collapse" in pn.dismissed

    run_async(go())


# -------------------------------------------------------- tuning timeout


def test_a_miner_that_never_finishes_tuning_gets_the_wanted_power_as_its_limit(monkeypatch):
    rig = v3_rig(monkeypatch, up=0, store={"power_limit_w": 5000, "regulated_power_w": 2000})
    rig.mining(True, limit=5000)
    rig.coord.data.update({"wattage": 3400})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.run(C.V3_TUNE_TIMEOUT_S / 60 - 1)
        assert rig.calls == []
        await rig.run(2)
        assert cmds(rig, "set_power_limit") == [2000]
        assert rig.ctl._ceiling_ok(rig.clock.t, frozenset(), 104.0) is not None  # held down
        await rig.run(60)
        assert cmds(rig, "set_power_limit") == [2000]  # once

    run_async(go())


# ---------------------------------------------------------- daily strategy


def test_daily_plan_is_delivered_by_set_miner_power(monkeypatch):
    rig = v3_rig(
        monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 3000},
        **{const.CONF_CONTROL_STRATEGY: "daily"},
    )
    rig.mining(True, limit=5000)
    rig.coord.data["wattage"] = 3000
    rig.set_supply(104.0)

    async def go():
        Store.saved[KEY]["daily_plan"] = {
            "power_w": 2500, "computed_at": rig.clock.t, "next_at": rig.clock.t + 86400,
        }
        await rig.setup()
        await rig.run(10)
        assert cmds(rig, "set_power_limit") == []
        assert cmds(rig, "set_miner_power")[-1] == 2500
        assert rig.pid_state["control_mode"] == "daily"

    run_async(go())


# ----------------------------------------------------------------- rollback


def test_rolling_back_puts_the_limit_back_at_the_regulated_power_once(monkeypatch):
    rig = Rig(monkeypatch)  # adjust_power_limit again
    Store.saved[KEY] = {"power_limit_w": 5000, "regulated_power_w": 2500}
    rig.mining(True, limit=5000)
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        assert rig.ctl._seed_bumpless_transfer() == 2500  # not the 5000 W ceiling
        await rig.tick()
        assert cmds(rig, "set_power_limit") == [2500]
        assert Store.saved[KEY]["regulated_power_w"] is None
        await rig.run(10)
        assert cmds(rig, "set_power_limit") == [2500]
        assert cmds(rig, "set_miner_power") == []

    run_async(go())


def test_legacy_users_who_never_enabled_v3_get_no_rollback_command(monkeypatch):
    rig = Rig(monkeypatch)
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert rig.ctl._rollback_power is None
        assert Store.saved[KEY]["regulated_power_w"] is None

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
