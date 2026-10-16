"""Daily strategy (1.10.0): one planned power per day instead of continuous PID.

P_day = P_ff + P_fb, with P_ff = P_avg24·(T_bal−T_fc24)/max(T_bal−T_out24, 5)
and P_fb = Kp_day·(target − 24 h mean supply while mining).
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import MIN, Rig, Store, const, controller_mod, run_async  # noqa: E402

HOUR = 3600.0
DAILY = {const.CONF_CONTROL_STRATEGY: "daily"}
OBSERVE = {const.CONF_CONTROL_STRATEGY: "daily_observe"}


def fill(rig, hours=24, power=3000.0, supply=104.0, outdoor=35.0):
    """Hourly aggregates for the trailing `hours` (6 samples per hour)."""
    now = rig.clock.t
    hour = now - now % HOUR
    rig.ctl._daily_buckets = [
        {"t": hour - h * HOUR, "n": 6, "p": 6 * power, "ns": 6, "s": 6 * supply, "no": 6, "o": 6 * outdoor}
        for h in range(hours, 0, -1)
    ]


def forecast(rig, temp, hours=24):
    return [(rig.clock.t + HOUR * (i + 1), float(temp)) for i in range(hours)]


def plan(rig, fc):
    rig.ctl._compute_daily_plan(rig.clock.t, fc)
    return rig.ctl._daily_plan


def limits(rig):
    return [w for c, w in rig.calls if c == "set_power_limit"]


def track_limit(rig):
    """Make the fake miner report the limit it was last sent."""
    orig = rig.coord.api.set_power_limit

    async def wrapped(watts):
        await orig(watts)
        rig.coord.data["wattage_limit"] = int(watts)
        rig.coord.data["wattage"] = int(watts)

    rig.coord.api.set_power_limit = wrapped


# ------------------------------------------------------------------ formula


def test_warmer_forecast_plans_less_power(monkeypatch):
    rig = Rig(monkeypatch, **OBSERVE)
    rig.coord.data["wattage_limit"] = 3000
    fill(rig, power=3000.0, supply=104.0, outdoor=35.0)
    same = plan(rig, forecast(rig, 35.0))
    assert same["power_w"] == 3000 and same["p_ff"] == 3000.0 and same["p_fb"] == 0.0, same
    rig.ctl._daily_plan = None
    warmer = plan(rig, forecast(rig, 45.0))
    # 3000·(65−45)/(65−35) = 2000
    assert warmer["power_w"] == 2000 and warmer["t_fc24"] == 45.0 and warmer["t_out24"] == 35.0, warmer
    rig.ctl._daily_plan = None
    colder = plan(rig, forecast(rig, 25.0))
    assert colder["power_w"] == 4000, colder


def test_supply_above_target_plans_less_power(monkeypatch):
    rig = Rig(monkeypatch, **OBSERVE)
    rig.coord.data["wattage_limit"] = 3000
    fill(rig, power=3000.0, supply=110.0, outdoor=35.0)  # target 104
    p = plan(rig, forecast(rig, 35.0))
    # P_fb = 60·(104−110) = −360 → 2640 → rounded to 2650
    assert p["p_fb"] == -360.0 and p["power_w"] == 2650 and p["supply_mean24"] == 110.0, p


def test_missing_outdoor_data_uses_mean_power(monkeypatch):
    rig = Rig(monkeypatch, **OBSERVE)
    rig.coord.data["wattage_limit"] = 3000
    fill(rig, power=3000.0, supply=104.0, outdoor=35.0)
    p = plan(rig, [])  # no forecast
    assert p["p_ff"] == 3000.0 and p["t_fc24"] is None and p["power_w"] == 3000, p


def test_plan_is_no_longer_step_limited_from_the_previous_plan(monkeypatch):
    """1.13.0 removed the ±750 W/day plan move limit for both daily strategies."""
    rig = Rig(monkeypatch, **OBSERVE)
    rig.coord.data["wattage_limit"] = 3000
    fill(rig, power=3000.0, supply=104.0, outdoor=35.0)
    first = plan(rig, forecast(rig, 35.0))
    assert first["power_w"] == 3000
    second = plan(rig, forecast(rig, 55.0))  # raw 3000·10/30 = 1000
    assert second["power_w"] == 1000, second
    assert "step-limited" not in second["reason"]
    # And clamped to the effective floor / power_max.
    fill(rig, power=5000.0, supply=80.0, outdoor=35.0)
    rig.ctl._daily_plan = {**second, "power_w": 4900}
    assert plan(rig, forecast(rig, 20.0))["power_w"] == 5000


def test_insufficient_samples_hold_the_current_limit_and_retry_within_an_hour(monkeypatch):
    rig = Rig(monkeypatch, **OBSERVE)
    rig.coord.data["wattage_limit"] = 2750
    fill(rig, hours=6, power=4000.0)
    p = plan(rig, forecast(rig, 20.0))
    assert p["power_w"] == 2750 and p["samples_h"] == 6 and p["p_ff"] is None, p
    assert "need 12" in p["reason"]
    assert p["next_at"] <= rig.clock.t + controller_mod.DAILY_RETRY_S


def test_next_plan_is_at_the_local_plan_hour(monkeypatch):
    rig = Rig(monkeypatch, **{**OBSERVE, const.CONF_DAILY_PLAN_HOUR: 4})
    fill(rig)
    p = plan(rig, forecast(rig, 35.0))
    nxt = p["next_at"]
    assert nxt % 86400 == 4 * HOUR  # tests run in UTC
    assert 0 < nxt - rig.clock.t <= 86400
    assert rig.pid_state["daily_plan"]["next_at"] is not None


# ------------------------------------------------------------------ acting


def test_daily_issues_one_command_per_plan_and_nothing_between(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    track_limit(rig)
    rig.coord.data["wattage_limit"] = 3000
    fill(rig, power=3000.0, supply=104.0, outdoor=55.0)
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        plans: list[float] = []
        sends: list[tuple[float, int]] = []
        n_calls = 0
        for _ in range(int(50 * HOUR / 300)):
            # The supply wanders around target; the PID would chase this.
            rig.set_supply(100.0 + (len(plans) % 3) * 4.0)
            await rig.tick(300)
            computed = rig.ctl._daily_plan["computed_at"]
            if not plans or plans[-1] != computed:
                plans.append(computed)
            if len(rig.calls) != n_calls:
                sends.append((rig.clock.t, limits(rig)[-1]))
                n_calls = len(rig.calls)
        assert rig.pid_state["control_mode"] == "daily"
        assert len(plans) >= 3  # startup plan + two plan hours
        assert len(sends) <= len(plans), (sends, plans)
        # No two commands under the same plan.
        assert len({max(p for p in plans if p <= t) for t, _ in sends}) == len(sends), (sends, plans)
        assert ("power_off", None) not in rig.calls
        assert rig.pid_state["soft_start"] is False
        assert rig.pid_state["daily_plan"]["strategy"] == "daily"
        assert Store.saved["whatsminer.e1.controller"]["daily_plan"]["power_w"] is not None
        assert len(Store.saved["whatsminer.e1.controller"]["daily_buckets"]) >= 24

    run_async(go())


def test_daily_plan_within_deadband_sends_nothing(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    rig.coord.data["wattage_limit"] = 4200
    fill(rig, power=4150.0, supply=104.0, outdoor=55.0)  # plan 4150, 50 W away
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.run(60, step_s=60)
        assert rig.ctl._daily_plan["power_w"] == 4150
        assert limits(rig) == [], rig.calls
        assert rig.pid_state["control_mode"] == "daily"

    run_async(go())


def test_daily_caps_clamp_and_release_returns_to_the_plan_once(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    track_limit(rig)
    rig.coord.data["wattage_limit"] = 3000
    fill(rig, power=3000.0, supply=104.0, outdoor=55.0)
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.run(10)
        assert limits(rig) == [], rig.calls  # plan 3000 = limit in force
        rig.set_supply(123.0)  # over the 122°F supply cap
        await rig.run(10)
        assert limits(rig) == [1000], rig.calls
        assert rig.pid_state["control_mode"] == "safety_cap"
        rig.set_supply(104.0)
        await rig.run(60)
        assert limits(rig) == [1000, 3000], rig.calls
        assert rig.pid_state["control_mode"] == "daily"

    run_async(go())


def test_daily_observe_computes_and_publishes_but_never_commands(monkeypatch):
    """Observe mode: the plan is published; commands are exactly the PID's."""

    async def drive(rig):
        rig.coord.data["wattage_limit"] = 3000
        fill(rig, power=2000.0, supply=104.0, outdoor=55.0)  # a plan far from 3000
        await rig.setup()
        for i in range(int(30 * HOUR / 300)):
            rig.set_supply(96.0 + (i % 5) * 3.0)
            await rig.tick(300)
        return list(rig.calls)

    pid_rig = Rig(monkeypatch)
    pid_calls = run_async(drive(pid_rig))
    assert pid_rig.pid_state["daily_plan"]["power_w"] is None  # pid computes nothing

    obs_rig = Rig(monkeypatch, **OBSERVE)
    obs_calls = run_async(drive(obs_rig))
    assert obs_calls == pid_calls
    assert pid_calls, "the PID should have acted on the wandering supply"
    published = obs_rig.pid_state["daily_plan"]
    assert published["strategy"] == "daily_observe" and published["power_w"] is not None
    assert published["samples_h"] >= 12
    assert obs_rig.pid_state["control_mode"] != "daily"


def test_daily_plan_and_buckets_survive_a_restart(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    rig.coord.data["wattage_limit"] = 3000
    fill(rig, power=3000.0, supply=104.0, outdoor=55.0)

    async def go():
        await rig.setup()
        await rig.tick()
        await rig.ctl._save()
        saved = dict(Store.saved["whatsminer.e1.controller"])
        first = dict(rig.ctl._daily_plan)
        rig2 = Rig(monkeypatch, **DAILY)
        rig2.clock.t = rig.clock.t + 5 * MIN
        Store.saved["whatsminer.e1.controller"] = saved
        await rig2.setup()
        assert rig2.ctl._daily_plan["computed_at"] == first["computed_at"]
        assert len(rig2.ctl._daily_buckets) >= 24
        assert not rig2.ctl._daily_due(rig2.clock.t)

    run_async(go())
