"""daily_trim strategy (1.13.0): the PID runs as under `pid`, with its output
envelope intersected each tick with [P_day - 500, P_day + 500] once a valid
plan exists. The band only narrows; every cap and lockout still wins.

A plan computed while samples are short (<12 h) holds the current limit and
has no feedforward — it does not arm the band, and the loop is exactly pid
until the first real plan lands.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import MIN, Rig, const, run_async  # noqa: E402
from test_daily import fill, limits, track_limit  # noqa: E402

HOUR = 3600.0
TRIM = {const.CONF_CONTROL_STRATEGY: "daily_trim"}
BAND = const.DAILY_TRIM_BAND


def trim_rig(monkeypatch, plan_power=2000, **over):
    """A daily_trim rig whose startup plan computes to `plan_power`."""
    rig = Rig(monkeypatch, **{**TRIM, **over})
    track_limit(rig)
    rig.coord.data["wattage_limit"] = plan_power
    fill(rig, power=float(plan_power), supply=104.0, outdoor=55.0)
    return rig


# ------------------------------------------------------------------ envelope


def test_trim_band_clamps_the_pid_to_plan_plus_band(monkeypatch):
    """Cold supply: the PID wants far more than the band allows."""

    async def drive(rig):
        await rig.setup()
        rig.set_supply(104.0)  # at target, so no soft start arms
        await rig.run(5)
        assert rig.ctl._daily_plan["power_w"] == 2000
        for i in range(int(120 * MIN / 300)):
            rig.set_supply(90.0 + (i % 3))  # way below target, with dither
            await rig.tick(300)
        return [w for c, w in rig.calls if c == "set_power_limit" and w > 2000]

    pid_rig = Rig(monkeypatch)
    track_limit(pid_rig)
    pid_rig.coord.data["wattage_limit"] = 2000

    trim_rig_ = trim_rig(monkeypatch)
    trim_calls = run_async(drive(trim_rig_))
    pid_calls = run_async(drive(pid_rig))

    assert pid_calls and max(pid_calls) > 2000 + BAND, pid_calls
    assert trim_calls, trim_calls
    assert max(trim_calls) == 2000 + BAND, trim_calls
    assert min(trim_calls) >= 2000 - BAND, trim_calls
    assert trim_calls[-1] == 2000 + BAND
    assert trim_rig_.pid_state["out_max_effective"] == 2000 + BAND
    assert trim_rig_.pid_state["daily_plan"]["trim_active"] is True


def test_empty_interval_against_the_band_is_plain_pid(monkeypatch):
    """A price envelope at zero leaves out_max = floor, wholly below the band:
    the intersection would be empty, so the band is skipped and the tick is
    exactly what pid does.
    """
    price = {
        const.CONF_PID_PRICE_SENSOR: "sensor.price",
        const.CONF_PID_PRICE_HIGH: 30.0,
        const.CONF_PID_PRICE_LOW: 10.0,
    }

    async def drive(rig):
        rig.hass.states.items["sensor.price"] = ha_state("30.0")
        await rig.setup()
        await rig.run(30)
        return list(rig.calls)

    from ha_stubs import FakeState as ha_state  # noqa: PLC0415

    pid_rig = Rig(monkeypatch, **price)
    track_limit(pid_rig)
    pid_rig.coord.data["wattage_limit"] = 2000
    pid_calls = run_async(drive(pid_rig))

    trim_rig_ = trim_rig(monkeypatch, **price)
    trim_calls = run_async(drive(trim_rig_))

    assert trim_calls == pid_calls, (trim_calls, pid_calls)
    # The band did not lift the pinned envelope (out_max stays at the floor).
    assert trim_rig_.pid_state["out_max_effective"] == pid_rig.pid_state["out_max_effective"]


def test_no_valid_plan_is_exactly_pid(monkeypatch):
    """Parity: under 12 h of samples there is no real plan and every command
    matches plain pid. (Past 12 h the first plan legitimately lands, so this
    drive stays under that.)
    """

    async def drive(rig):
        rig.coord.data["wattage_limit"] = 3000
        # No fill(): the plan defers (holds the limit, no feedforward).
        await rig.setup()
        for i in range(int(10 * HOUR / 300)):
            rig.set_supply(96.0 + (i % 5) * 3.0)
            await rig.tick(300)
        return list(rig.calls)

    pid_rig = Rig(monkeypatch)
    pid_calls = run_async(drive(pid_rig))

    trim_rig_ = Rig(monkeypatch, **TRIM)
    trim_calls = run_async(drive(trim_rig_))

    assert trim_calls == pid_calls, (trim_calls, pid_calls)
    assert pid_calls, "the PID should have acted on the wandering supply"
    assert trim_rig_.pid_state["daily_plan"]["trim_active"] is False


def test_plan_update_reclamps_on_the_next_tick(monkeypatch):
    rig = trim_rig(monkeypatch)

    async def go():
        await rig.setup()
        rig.set_supply(90.0)
        await rig.run(120)
        assert limits(rig) and limits(rig)[-1] == 2500, rig.calls
        # A new, lower plan lands mid-run (2000 -> 1500 W).
        rig.ctl._daily_plan = {
            **rig.ctl._daily_plan, "power_w": 1500, "next_at": rig.clock.t + 86400,
        }
        rig.ctl._publish_daily()
        await rig.tick(30)
        assert rig.pid_state["out_max_effective"] == 1500 + BAND
        assert rig.ctl._pid.out_max == 1500 + BAND
        assert rig.pid_state["daily_plan"]["trim_active"] is True
        await rig.run(60)
        assert limits(rig)[-1] == 1500 + BAND, limits(rig)

    run_async(go())


def test_supply_cap_still_clamps_below_the_band(monkeypatch):
    rig = trim_rig(monkeypatch)

    async def go():
        await rig.setup()
        await rig.run(30)
        rig.set_supply(123.0)  # over the 122°F supply cap
        await rig.run(10)
        # The cap clamp (floor) wins even though the band floor is 1500.
        assert limits(rig)[-1] == 1000, limits(rig)
        assert rig.pid_state["control_mode"] == "safety_cap"
        rig.set_supply(104.0)
        await rig.run(60)
        # Released: the loop heads back up, but never past the band.
        assert limits(rig)[-1] <= 2000 + BAND, limits(rig)

    run_async(go())


def test_trim_attributes_published(monkeypatch):
    rig = trim_rig(monkeypatch)

    async def go():
        await rig.setup()
        await rig.run(5)
        published = rig.pid_state["daily_plan"]
        assert published["strategy"] == "daily_trim"
        assert published["power_w"] == 2000
        assert published["trim_active"] is True
        assert published["reason"]

    run_async(go())

    pid_rig = Rig(monkeypatch)

    async def go_pid():
        await pid_rig.setup()
        await pid_rig.tick()
        assert pid_rig.pid_state["daily_plan"]["trim_active"] is False

    run_async(go_pid())
