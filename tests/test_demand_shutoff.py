"""Unit tests for the pure demand-shutoff / freeze-guard state machine."""
from __future__ import annotations

import pytest

from conftest import demand_shutoff as ds

MIN = 60.0
T0 = 1_000_000.0

CFG = ds.ShutoffConfig(mode=ds.MODE_ACTIVE, has_entities=True)


def inputs(**kw) -> ds.ShutoffInputs:
    base = dict(
        summary=ds.ALL_IDLE,
        gate_mean=59.0,
        supply=100.0,
        target=104.0,
        is_mining=True,
        fresh=True,
        latched=False,
        freeze=False,
    )
    base.update(kw)
    return ds.ShutoffInputs(**base)


def run(cfg, st, inp, start, minutes, step=0.5):
    """Tick every ``step`` minutes for ``minutes``; return (decisions, state)."""
    decisions = []
    t = start
    end = start + minutes * MIN
    while t <= end + 1e-9:
        d = ds.decide(cfg, st, inp, t)
        decisions.append((t, d))
        st = d.state
        t += step * MIN
    return decisions, st


def stopped_state(now=T0, owned=True, gate_armed=True) -> ds.ShutoffState:
    return ds.ShutoffState(
        state=ds.STOPPED, owned=owned, since=now, last_stop_at=now, gate_armed=gate_armed
    )


# --- 1. W trigger and dwell ----------------------------------------------------


def test_w_stop_after_dwell_not_before():
    st = ds.ShutoffState()
    decisions, st = run(CFG, st, inputs(), T0, 29)
    assert all(d.action == ds.NONE for _, d in decisions)
    assert st.state == ds.DWELL
    d = ds.decide(CFG, st, inputs(), T0 + 30 * MIN)
    assert d.action == ds.STOP
    assert d.state.state == ds.STOPPED and d.state.owned


def test_calling_tick_resets_dwell():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(), T0, 20)
    d = ds.decide(CFG, st, inputs(summary=ds.CALLING, calling=("climate.den",)), T0 + 21 * MIN)
    assert d.state.state == ds.RUNNING
    assert "climate.den heating" in d.blocking
    _, st = run(CFG, d.state, inputs(), T0 + 22 * MIN, 29)
    assert st.state == ds.DWELL  # had to re-earn the dwell


def test_unknown_blocks_w_stop():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(summary=ds.UNKNOWN, unknown=("climate.back_bedroom",)), T0, 60)
    assert st.state == ds.RUNNING


# --- 2. gate hysteresis -------------------------------------------------------


@pytest.mark.parametrize(
    "means,expected",
    [
        ([58.0], True),
        ([58.0, 55.0], True),
        ([58.0, 53.9], False),
        ([58.0, 53.9, 57.0], False),
        ([58.0, None], False),
        ([57.9], False),
    ],
)
def test_gate_hysteresis(means, expected):
    st = ds.ShutoffState()
    t = T0
    for m in means:
        d = ds.decide(CFG, st, inputs(summary=ds.CALLING, gate_mean=m), t)
        st = d.state
        t += MIN
    assert st.gate_armed is expected


# --- 3. S trigger -------------------------------------------------------------


def test_s_stop_cold_outdoor_and_bypasses_min_on():
    st = ds.ShutoffState(last_resume_at=T0 - 5 * MIN)  # min_on not satisfied
    _, st = run(CFG, st, inputs(gate_mean=40.0, supply=123.0), T0, 9)
    assert st.state == ds.DWELL and st.dwell_trigger == "S"
    d = ds.decide(CFG, st, inputs(gate_mean=40.0, supply=123.0), T0 + 10 * MIN)
    assert d.action == ds.STOP
    assert "(S)" in d.reason


def test_s_stop_with_unknown_thermostats():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(summary=ds.UNKNOWN, gate_mean=40.0, supply=123.0), T0, 9)
    d = ds.decide(CFG, st, inputs(summary=ds.UNKNOWN, gate_mean=40.0, supply=123.0), T0 + 10 * MIN)
    assert d.action == ds.STOP


def test_s_not_with_calling():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(summary=ds.CALLING, gate_mean=40.0, supply=123.0), T0, 30)
    assert st.state == ds.RUNNING


def test_s_disabled_by_config():
    cfg = ds.ShutoffConfig(mode=ds.MODE_ACTIVE, has_entities=True, supply_stop=False)
    st = ds.ShutoffState()
    _, st = run(cfg, st, inputs(gate_mean=40.0, supply=130.0), T0, 30)
    assert st.state == ds.RUNNING


def test_s_dwell_releases_below_cap_minus_5():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(gate_mean=40.0, supply=123.0), T0, 5)
    d = ds.decide(CFG, st, inputs(gate_mean=40.0, supply=118.0), T0 + 6 * MIN)
    assert d.state.state == ds.DWELL  # 118 ≥ 117, still dwelling
    d = ds.decide(CFG, d.state, inputs(gate_mean=40.0, supply=116.0), T0 + 7 * MIN)
    assert d.state.state == ds.RUNNING


# --- 4. min_on ----------------------------------------------------------------


def test_min_on_blocks_w_stop():
    st = ds.ShutoffState(last_resume_at=T0)
    _, st = run(CFG, st, inputs(), T0, 59)
    assert st.state == ds.DWELL
    d = ds.decide(CFG, st, inputs(), T0 + 59 * MIN)
    assert d.action == ds.NONE and any("min_on" in b for b in d.blocking)
    d = ds.decide(CFG, st, inputs(), T0 + 60 * MIN)
    assert d.action == ds.STOP


# --- 5. resume on calling -----------------------------------------------------


def test_resume_waits_for_min_off_and_debounce():
    st = stopped_state()
    inp = inputs(summary=ds.CALLING, calling=("climate.den",), is_mining=False)
    d = ds.decide(CFG, st, inp, T0 + 10 * MIN)
    assert d.action == ds.NONE and "confirming call" in d.blocking
    d = ds.decide(CFG, d.state, inp, T0 + 10.5 * MIN)
    assert d.action == ds.NONE and any("min_off" in b for b in d.blocking)
    d = ds.decide(CFG, d.state, inp, T0 + 30 * MIN)
    assert d.action == ds.RESUME
    assert d.state.state == ds.RESUMING


def test_single_calling_poll_does_not_resume():
    st = stopped_state()
    d = ds.decide(CFG, st, inputs(summary=ds.CALLING, is_mining=False), T0 + 40 * MIN)
    assert d.action == ds.NONE
    d = ds.decide(CFG, d.state, inputs(summary=ds.ALL_IDLE, is_mining=False), T0 + 40.5 * MIN)
    assert d.action == ds.NONE and d.state.call_polls == 0


def test_disarmed_gate_resumes_at_once():
    st = stopped_state()
    inp = inputs(summary=ds.CALLING, gate_mean=50.0, is_mining=False)
    d = ds.decide(CFG, st, inp, T0 + 2 * MIN)
    d = ds.decide(CFG, d.state, inp, T0 + 2.5 * MIN)
    assert d.action == ds.RESUME


# --- 6. unknown grace (fail-warm) --------------------------------------------


def test_unknown_grace_resumes_when_supply_at_or_below_target():
    st = stopped_state()
    inp = inputs(summary=ds.UNKNOWN, is_mining=False, supply=100.0, target=104.0)
    _, st = run(CFG, st, inp, T0 + MIN, 19)
    assert st.state == ds.STOPPED
    d = ds.decide(CFG, st, inp, T0 + 21 * MIN)
    assert d.action == ds.RESUME and "fail-warm" in d.reason


def test_unknown_grace_holds_when_supply_hot():
    st = stopped_state()
    inp = inputs(summary=ds.UNKNOWN, is_mining=False, supply=110.0, target=104.0)
    _, st = run(CFG, st, inp, T0 + MIN, 25)
    assert st.state == ds.STOPPED


# --- 7. thermostat classification --------------------------------------------


@pytest.mark.parametrize(
    "action,mode,cur,sp,age,avail,expected",
    [
        ("heating", "heat", 69, 70, 60, True, ds.CALLING),
        ("idle", "heat", 69.5, 70, 60, True, ds.IDLE),
        ("idle", "heat", 68.0, 70, 60, True, ds.CALLING),  # cold room delta 1.5
        ("off", "off", 60, 70, 60, True, ds.IDLE),
        ("idle", "off", 60, 70, 60, True, ds.IDLE),
        (None, "heat", 69, 70, 60, True, ds.UNKNOWN),
        ("idle", "heat", 69, 70, 60, False, ds.UNKNOWN),
        ("idle", "heat", 69, 70, 1801, True, ds.UNKNOWN),
        ("heating", "heat", 69, 70, 1801, True, ds.UNKNOWN),
        ("cooling", "cool", 75, 70, 60, True, ds.UNKNOWN),
    ],
)
def test_classify(action, mode, cur, sp, age, avail, expected):
    assert ds.classify_thermostat(action, mode, cur, sp, age, 1.5, avail) == expected


def test_cold_room_delta_zero_disables():
    assert ds.classify_thermostat("idle", "heat", 60, 70, 10, 0.0) == ds.IDLE


def test_summarize():
    assert ds.summarize({"a": ds.IDLE, "b": ds.IDLE}) == ds.ALL_IDLE
    assert ds.summarize({"a": ds.IDLE, "b": ds.UNKNOWN}) == ds.UNKNOWN
    assert ds.summarize({"a": ds.CALLING, "b": ds.UNKNOWN}) == ds.CALLING
    assert ds.summarize({}) == ds.UNKNOWN


# --- 8. stale data -----------------------------------------------------------


def test_stale_blocks_stop_but_allows_resume():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(), T0, 29)
    d = ds.decide(CFG, st, inputs(fresh=False), T0 + 31 * MIN)
    assert d.action == ds.NONE and "miner data stale" in d.blocking
    st = stopped_state()
    inp = inputs(summary=ds.CALLING, fresh=False, is_mining=False)
    d = ds.decide(CFG, st, inp, T0 + 40 * MIN)
    d = ds.decide(CFG, d.state, inp, T0 + 40.5 * MIN)
    assert d.action == ds.RESUME


def test_stale_never_releases_or_adopts():
    cfg_off = ds.ShutoffConfig(mode=ds.MODE_OFF, has_entities=True)
    # RELEASE depends only on config, not freshness — but it must not fire when
    # latched, and stale data must not fake an external start.
    st = stopped_state()
    d = ds.decide(cfg_off, st, inputs(latched=True, fresh=False), T0 + MIN)
    assert d.action == ds.NONE and d.state.owned is False
    st = stopped_state()
    for i in range(6):
        d = ds.decide(CFG, st, inputs(is_mining=True, fresh=False), T0 + 6 * MIN + i * 30)
        st = d.state
    assert d.action == ds.NONE and st.state == ds.STOPPED


# --- 9. external start: reassert then adopt ----------------------------------


def test_external_start_reasserts_three_times_then_adopts():
    st = stopped_state()
    inp = inputs(is_mining=True)
    t = T0 + 6 * MIN
    actions = []
    for i in range(3):
        d = ds.decide(CFG, st, inp, t + i * 30)
        st = d.state
        actions.append(d.action)
    assert actions == [ds.NONE, ds.NONE, ds.REASSERT]
    d = ds.decide(CFG, st, inp, t + 120)
    assert d.action == ds.NONE and "reassert pending" in d.blocking
    d = ds.decide(CFG, d.state, inp, t + 60 + 180)
    assert d.action == ds.REASSERT and d.state.reasserts == 2
    d = ds.decide(CFG, d.state, inp, t + 60 + 360)
    assert d.action == ds.REASSERT and d.state.reasserts == 3
    d = ds.decide(CFG, d.state, inp, t + 60 + 540)
    assert d.action == ds.ADOPT and d.notify == "adopted"
    assert d.state.owned is False and d.state.suppressed and d.state.state == ds.RUNNING
    assert d.state.adopted_at == t + 60 + 540


def test_own_slow_stop_is_reclaimed_after_adopt():
    """Miner kept reporting hashing for 15 min after our power_off, then stopped."""
    st = stopped_state()
    inp = inputs(is_mining=True)
    t = T0 + 6 * MIN
    d = None
    for i in range(40):
        d = ds.decide(CFG, st, inp, t + i * 30)
        st = d.state
        if d.action == ds.ADOPT:
            break
    assert d.action == ds.ADOPT
    # 2 minutes later it is actually off: take ownership back so it resumes
    d = ds.decide(CFG, st, inputs(is_mining=False), st.adopted_at + 120)
    assert d.state.state == ds.STOPPED and d.state.owned is True and not d.state.suppressed
    inp = inputs(summary=ds.CALLING, is_mining=False, gate_mean=50.0)
    d = ds.decide(CFG, d.state, inp, st.adopted_at + 150)
    d = ds.decide(CFG, d.state, inp, st.adopted_at + 180)
    assert d.action == ds.RESUME


def test_mining_blip_ignored():
    st = stopped_state()
    t = T0 + 6 * MIN
    d = ds.decide(CFG, st, inputs(is_mining=True), t)
    d = ds.decide(CFG, d.state, inputs(is_mining=True), t + 30)
    d = ds.decide(CFG, d.state, inputs(is_mining=False), t + 60)
    assert d.state.mining_polls == 0
    d = ds.decide(CFG, d.state, inputs(is_mining=True), t + 90)
    assert d.action == ds.NONE


def test_mining_right_after_stop_is_not_external():
    st = stopped_state()
    for i in range(5):
        d = ds.decide(CFG, st, inputs(is_mining=True), T0 + 30 * (i + 1))
        st = d.state
    assert d.action == ds.NONE and st.mining_polls == 0


# --- 10. latch -------------------------------------------------------------------


def test_latched_clears_ownership_and_never_resumes():
    st = stopped_state()
    inp = inputs(summary=ds.CALLING, latched=True, is_mining=False, gate_mean=40.0)
    d = ds.decide(CFG, st, inp, T0 + 40 * MIN)
    assert d.action == ds.NONE and d.state.owned is False and d.state.state == ds.RUNNING
    d = ds.decide(CFG, d.state, inp, T0 + 41 * MIN)
    assert d.action == ds.NONE


# --- 11. user actions -----------------------------------------------------------


def test_user_on_sets_suppressed_until_calling():
    st = stopped_state()
    st = ds.user_mining_on(st, T0 + MIN)
    assert st.owned is False and st.suppressed and st.state == ds.RUNNING
    _, st = run(CFG, st, inputs(), T0 + 2 * MIN, 45)
    assert st.state == ds.DWELL and st.suppressed  # dwelling but blocked
    d = ds.decide(CFG, st, inputs(), T0 + 48 * MIN)
    assert d.action == ds.NONE and "suppressed until a thermostat calls" in d.blocking
    d = ds.decide(CFG, d.state, inputs(summary=ds.CALLING), T0 + 49 * MIN)
    assert d.state.suppressed is False


def test_user_off_and_release_clear_ownership_without_power_on():
    st = stopped_state()
    st = ds.user_mining_off(st, T0 + MIN)
    assert st.owned is False and st.state == ds.RUNNING
    # Miner now off by the user: no resume ever happens.
    d = ds.decide(CFG, st, inputs(summary=ds.CALLING, is_mining=False), T0 + 60 * MIN)
    d = ds.decide(CFG, d.state, inputs(summary=ds.CALLING, is_mining=False), T0 + 61 * MIN)
    assert d.action == ds.NONE


# --- 12. observe mode -------------------------------------------------------------


def test_observe_mode_never_commands_but_reports():
    cfg = ds.ShutoffConfig(mode=ds.MODE_OBSERVE, has_entities=True)
    st = ds.ShutoffState()
    decisions, st = run(cfg, st, inputs(), T0, 31)
    assert all(d.action == ds.NONE for _, d in decisions)
    assert st.state == ds.STOPPED and st.simulated and not st.owned
    assert st.would_stop_at == T0 + 30 * MIN
    inp = inputs(summary=ds.CALLING, is_mining=True)
    d = ds.decide(cfg, st, inp, T0 + 61 * MIN)
    d = ds.decide(cfg, d.state, inp, T0 + 61.5 * MIN)
    assert d.action == ds.NONE and d.state.state == ds.RUNNING
    assert d.state.would_resume_at == T0 + 61.5 * MIN


def test_observe_stopped_is_not_treated_as_external_start():
    cfg = ds.ShutoffConfig(mode=ds.MODE_OBSERVE, has_entities=True)
    st = ds.ShutoffState(state=ds.STOPPED, simulated=True, since=T0, gate_armed=True)
    for i in range(10):
        d = ds.decide(cfg, st, inputs(is_mining=True), T0 + 10 * MIN + i * 30)
        st = d.state
        assert d.action == ds.NONE
    assert st.state == ds.STOPPED


# --- 13. store round trip / mode off release ------------------------------------


def test_store_round_trip_and_dwell_restores_as_running():
    st = ds.ShutoffState(state=ds.DWELL, dwell_start=T0, dwell_trigger="W", gate_armed=True)
    back = ds.ShutoffState.from_dict(st.to_dict())
    assert back.state == ds.RUNNING and back.dwell_start is None
    st = stopped_state()
    back = ds.ShutoffState.from_dict(st.to_dict())
    assert back == st
    assert ds.ShutoffState.from_dict(None) == ds.ShutoffState()
    assert ds.ShutoffState.from_dict({"state": ds.STOPPED, "bogus": 1}).state == ds.STOPPED


def test_mode_off_with_owned_stop_releases_and_retries_until_hashing():
    cfg = ds.ShutoffConfig(mode=ds.MODE_OFF, has_entities=True)
    d = ds.decide(cfg, stopped_state(), inputs(is_mining=False), T0 + MIN)
    assert d.action == ds.RELEASE and d.state.owned is True and d.state.state == ds.RESUMING
    # Still not hashing: wait, then retry the release after RELEASE_RETRY_S
    d = ds.decide(cfg, d.state, inputs(is_mining=False), T0 + 2 * MIN)
    assert d.action == ds.NONE
    d = ds.decide(cfg, d.state, inputs(is_mining=False), T0 + 3.5 * MIN)
    assert d.action == ds.RELEASE and d.state.resume_attempts == 2
    # Hashing confirmed → ownership released
    d = ds.decide(cfg, d.state, inputs(is_mining=True), T0 + 4 * MIN)
    assert d.action == ds.NONE and d.state.owned is False and d.state.state == ds.RUNNING


def test_unlatch_owns_the_restart_and_verifies():
    st = ds.ShutoffState()
    d = ds.decide(CFG, st, inputs(latched=True, freeze=True, supply=118.0, is_mining=False), T0)
    assert d.action == ds.UNLATCH and d.state.owned and d.state.state == ds.RESUMING
    # Latch cleared by the controller; power_on failed → retry after verify window
    d = ds.decide(CFG, d.state, inputs(latched=False, freeze=True, is_mining=False), T0 + 10 * MIN)
    assert d.action == ds.RESUME
    d = ds.decide(CFG, d.state, inputs(latched=False, freeze=True, is_mining=True), T0 + 11 * MIN)
    assert d.state.state == ds.RUNNING and d.state.owned is False


def test_external_start_with_demand_is_taken_as_resume_not_reasserted():
    st = stopped_state()
    inp = inputs(is_mining=True, summary=ds.CALLING, calling=("climate.den",))
    t = T0 + 6 * MIN
    d = None
    for i in range(3):
        d = ds.decide(CFG, st, inp, t + i * 30)
        st = d.state
    assert d.action == ds.NONE and st.state == ds.RUNNING and st.owned is False
    assert st.last_resume_at == t + 60
    # Same with freeze risk and idle thermostats
    st = stopped_state()
    inp = inputs(is_mining=True, freeze=True)
    for i in range(3):
        d = ds.decide(CFG, st, inp, t + i * 30)
        st = d.state
    assert d.action == ds.NONE and st.state == ds.RUNNING


def test_no_entities_is_inactive():
    cfg = ds.ShutoffConfig(mode=ds.MODE_ACTIVE, has_entities=False)
    _, st = run(cfg, ds.ShutoffState(), inputs(), T0, 60)
    assert st.state == ds.RUNNING


# --- resume verification -----------------------------------------------------------


def test_resume_confirmed_on_fresh_mining():
    st = ds.ShutoffState(state=ds.RESUMING, owned=True, since=T0, resume_sent_at=T0, resume_attempts=1)
    d = ds.decide(CFG, st, inputs(is_mining=False), T0 + MIN)
    assert d.action == ds.NONE and d.state.state == ds.RESUMING
    d = ds.decide(CFG, d.state, inputs(is_mining=True), T0 + 2 * MIN)
    assert d.state.state == ds.RUNNING and d.state.owned is False


def test_resume_retries_every_10_min_and_notifies_after_3():
    st = ds.ShutoffState(state=ds.RESUMING, owned=True, since=T0, resume_sent_at=T0, resume_attempts=1)
    inp = inputs(is_mining=False)
    d = ds.decide(CFG, st, inp, T0 + 9 * MIN)
    assert d.action == ds.NONE
    d = ds.decide(CFG, d.state, inp, T0 + 10 * MIN)
    assert d.action == ds.RESUME and d.state.resume_attempts == 2 and d.notify is None
    d = ds.decide(CFG, d.state, inp, T0 + 20 * MIN)
    assert d.action == ds.RESUME and d.notify is None
    d = ds.decide(CFG, d.state, inp, T0 + 30 * MIN)
    assert d.action == ds.RESUME and d.notify == "resume_failed"


def test_stop_send_failure_keeps_ownership_and_retries():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(), T0, 30)
    assert st.state == ds.STOPPED
    st = ds.mark_stop_failed(st, T0 + 30 * MIN)
    assert st.state == ds.STOPPED and st.owned
    # Still hashing: retry after STOP_RETRY_S
    d = ds.decide(CFG, st, inputs(is_mining=True), T0 + 31 * MIN)
    assert d.action == ds.NONE and "retrying stop shortly" in d.blocking
    d = ds.decide(CFG, d.state, inputs(is_mining=True), T0 + 33.5 * MIN)
    assert d.action == ds.STOP and d.state.owned
    # The command landed after all: fresh not-mining clears the flag, still owned
    d = ds.decide(CFG, d.state, inputs(is_mining=False), T0 + 34 * MIN)
    assert d.state.stop_failed_at is None and d.state.owned and d.state.state == ds.STOPPED
    # ...and a call later resumes it (the orphan case from the review)
    inp = inputs(summary=ds.CALLING, is_mining=False, gate_mean=50.0)
    d = ds.decide(CFG, d.state, inp, T0 + 35 * MIN)
    d = ds.decide(CFG, d.state, inp, T0 + 35.5 * MIN)
    assert d.action == ds.RESUME


def test_stop_send_failure_with_heat_wanted_goes_back_to_running():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(), T0, 30)
    st = ds.mark_stop_failed(st, T0 + 30 * MIN)
    d = ds.decide(CFG, st, inputs(is_mining=True, summary=ds.CALLING), T0 + 34 * MIN)
    assert d.action == ds.NONE and d.state.state == ds.RUNNING and not d.state.owned


def test_max_off_notice():
    st = stopped_state()
    d = ds.decide(CFG, st, inputs(is_mining=False), T0 + 24 * 3600 + MIN)
    assert d.notify == "max_off"


# --- freeze guard ---------------------------------------------------------------------


def test_freeze_blocks_w_stop():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(freeze=True), T0, 60)
    assert st.state == ds.DWELL
    d = ds.decide(CFG, st, inputs(freeze=True), T0 + 61 * MIN)
    assert d.action == ds.NONE and "freeze guard" in d.blocking


def test_freeze_beats_trigger_s():
    st = ds.ShutoffState()
    inp = inputs(gate_mean=30.0, supply=135.0, freeze=True)
    decisions, st = run(CFG, st, inp, T0, 60)
    assert all(d.action == ds.NONE for _, d in decisions)
    # The moment the freeze clears, S proceeds (dwell already elapsed).
    d = ds.decide(CFG, st, inputs(gate_mean=30.0, supply=135.0, freeze=False), T0 + 61 * MIN)
    assert d.action == ds.STOP


def test_freeze_force_resumes_immediately():
    st = stopped_state()
    # 2 minutes after the stop, all idle, min_off not met, single poll.
    d = ds.decide(CFG, st, inputs(summary=ds.ALL_IDLE, is_mining=False, freeze=True), T0 + 2 * MIN)
    assert d.action == ds.RESUME and "freeze guard" in d.reason
    assert d.state.state == ds.RESUMING


def test_freeze_resume_waits_for_hot_supply():
    st = stopped_state()
    d = ds.decide(CFG, st, inputs(is_mining=False, freeze=True, supply=125.0), T0 + 2 * MIN)
    assert d.action == ds.NONE and any("supply" in b for b in d.blocking)
    d = ds.decide(CFG, d.state, inputs(is_mining=False, freeze=True, supply=121.0), T0 + 3 * MIN)
    assert d.action == ds.RESUME


def test_freeze_resume_ignores_unknown_thermostats_and_hot_target():
    st = stopped_state()
    d = ds.decide(
        CFG, st, inputs(summary=ds.UNKNOWN, is_mining=False, freeze=True, supply=110.0, target=104.0),
        T0 + 2 * MIN,
    )
    assert d.action == ds.RESUME


def test_freeze_unlatches_when_supply_below_cap():
    st = ds.ShutoffState()
    d = ds.decide(CFG, st, inputs(latched=True, freeze=True, supply=130.0, is_mining=False), T0)
    assert d.action == ds.NONE
    d = ds.decide(CFG, d.state, inputs(latched=True, freeze=True, supply=118.0, is_mining=False), T0 + MIN)
    assert d.action == ds.UNLATCH


def test_freeze_unknown_blocks_stop_under_cold_gate_only():
    # Cold gate (mean 40) + S trigger + freeze unknown → blocked.
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(gate_mean=40.0, supply=125.0, freeze=None), T0, 30)
    assert st.state == ds.DWELL
    d = ds.decide(CFG, st, inputs(gate_mean=40.0, supply=125.0, freeze=None), T0 + 31 * MIN)
    assert d.action == ds.NONE and "freeze status unknown (cold gate)" in d.blocking
    # Warm gate + freeze unknown → W stop allowed.
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(freeze=None), T0, 29)
    d = ds.decide(CFG, st, inputs(freeze=None), T0 + 30 * MIN)
    assert d.action == ds.STOP


@pytest.mark.parametrize(
    "value,prev,expected",
    [
        (None, False, None),
        (40.0, False, True),
        (39.0, False, True),
        (41.0, False, False),
        (41.0, True, True),     # inside hysteresis, stays active
        (42.9, True, True),
        (43.0, True, False),    # released at threshold + 3
        (50.0, True, False),
    ],
)
def test_freeze_status(value, prev, expected):
    assert ds.freeze_status(value, 40.0, 3.0, prev) is expected


def test_freeze_fallback_value_uses_min_of_now_and_forecast():
    assert ds.freeze_fallback_value(55.0, [54, 47, 50]) == 47.0
    assert ds.freeze_fallback_value(45.0, [54, 47]) == 45.0
    assert ds.freeze_fallback_value(None, [54, 47]) == 47.0
    assert ds.freeze_fallback_value(45.0, None) == 45.0
    assert ds.freeze_fallback_value(None, []) is None


# --- centred mean -----------------------------------------------------------------------


def test_centred_mean_needs_trailing_samples_and_enough_points():
    now = T0
    samples = [(now - h * 3600, 50.0) for h in range(0, 12)]
    forecast = [(now + h * 3600, 70.0) for h in range(1, 13)]
    assert ds.centred_mean(samples, forecast, now) == pytest.approx(60.0)
    assert ds.centred_mean([], forecast, now) is None          # no trailing data
    assert ds.centred_mean(samples[:3], [], now) is None        # too few points
    # Stale samples outside the window are ignored.
    old = [(now - 20 * 3600, 10.0)] * 12
    assert ds.centred_mean(old, forecast, now) is None
    assert ds.trim_samples(old + samples, now) == samples


# --- reviewer-driven cases ----------------------------------------------------------


def test_unlatch_works_with_shutoff_mode_off():
    cfg = ds.ShutoffConfig(mode=ds.MODE_OFF, has_entities=True)
    d = ds.decide(cfg, ds.ShutoffState(), inputs(latched=True, freeze=True, supply=110.0, is_mining=False), T0)
    assert d.action == ds.UNLATCH and d.state.owned
    # Latch cleared, not hashing yet: release path retries until it hashes
    d = ds.decide(cfg, d.state, inputs(latched=False, is_mining=False), T0 + MIN)
    assert d.action == ds.NONE and "waiting for hashing" in d.blocking
    d = ds.decide(cfg, d.state, inputs(latched=False, is_mining=False), T0 + 3 * MIN)
    assert d.action == ds.RELEASE
    d = ds.decide(cfg, d.state, inputs(latched=False, is_mining=True), T0 + 4 * MIN)
    assert d.state.owned is False and d.state.state == ds.RUNNING


def test_observe_mode_releases_an_inherited_real_stop():
    cfg = ds.ShutoffConfig(mode=ds.MODE_OBSERVE, has_entities=True)
    d = ds.decide(cfg, stopped_state(), inputs(is_mining=False), T0 + MIN)
    assert d.action == ds.RELEASE and d.state.owned
    d = ds.decide(cfg, d.state, inputs(is_mining=True), T0 + 2 * MIN)
    assert d.state.owned is False and d.state.state == ds.RUNNING
    # From here on observe never commands
    _, st = run(cfg, d.state, inputs(), T0 + 3 * MIN, 61)
    assert st.simulated and st.state == ds.STOPPED


def test_freeze_unknown_near_latch_allows_owned_s_stop():
    st = ds.ShutoffState()
    inp = inputs(gate_mean=40.0, supply=136.0, freeze=None)
    _, st = run(CFG, st, inp, T0, 9)
    d = ds.decide(CFG, st, inp, T0 + 10 * MIN)
    assert d.action == ds.STOP and "(S)" in d.reason
    # Stopped, loop still hot, freeze unknown: wait for it to cool, then resume
    d = ds.decide(CFG, d.state, inputs(gate_mean=40.0, supply=128.0, freeze=None, is_mining=False), T0 + 12 * MIN)
    assert d.action == ds.NONE
    d = ds.decide(CFG, d.state, inputs(gate_mean=40.0, supply=119.0, freeze=None, is_mining=False), T0 + 25 * MIN)
    assert d.action == ds.RESUME and "freeze status unknown" in d.reason


def test_freeze_unknown_requires_margin_above_threshold_when_gate_lowered():
    cfg = ds.ShutoffConfig(mode=ds.MODE_ACTIVE, has_entities=True, outdoor_min=50.0, freeze_threshold=40.0)
    st = ds.ShutoffState()
    _, st = run(cfg, st, inputs(gate_mean=52.0, freeze=None), T0, 31)
    assert st.state == ds.DWELL  # 52 < 40 + 15: blocked
    d = ds.decide(cfg, st, inputs(gate_mean=52.0, freeze=None), T0 + 32 * MIN)
    assert d.action == ds.NONE and "freeze status unknown (cold gate)" in d.blocking
    st = ds.ShutoffState()
    _, st = run(cfg, st, inputs(gate_mean=56.0, freeze=None), T0, 29)
    d = ds.decide(cfg, st, inputs(gate_mean=56.0, freeze=None), T0 + 30 * MIN)
    assert d.action == ds.STOP


def test_user_on_always_suppresses_until_calling():
    st = ds.user_mining_on(ds.ShutoffState(), T0)
    assert st.suppressed
    _, st = run(CFG, st, inputs(), T0, 45)
    assert st.state == ds.DWELL
    d = ds.decide(CFG, st, inputs(), T0 + 46 * MIN)
    assert d.action == ds.NONE and "suppressed until a thermostat calls" in d.blocking


def test_user_off_blocks_stop_while_hashrate_lingers():
    st = ds.ShutoffState()
    _, st = run(CFG, st, inputs(), T0, 29)
    d = ds.decide(CFG, st, inputs(user_off=True), T0 + 30 * MIN)
    assert d.action == ds.NONE and "user turned mining off" in d.blocking


def test_centred_mean_requires_six_trailing_samples():
    now = T0
    forecast = [(now + h * 3600, 70.0) for h in range(1, 13)]
    few = [(now - h * 3600, 50.0) for h in range(0, 5)]
    assert ds.centred_mean(few, forecast, now) is None
    enough = [(now - h * 3600, 50.0) for h in range(0, 6)]
    assert ds.centred_mean(enough, forecast, now) == pytest.approx((6 * 50 + 12 * 70) / 18)
