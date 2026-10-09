"""A simple boiler-loop plant for replay-style controller tests.

One lumped loop temperature T (°F), time in minutes:

    C · dT/dt = Q − K_AMB · (T − T_AMB) − calling · UA_ZONE · (T − T_ROOM)

Fitted (grid search) to the 2026-10-08 18:07-19:44 resume: probe
sensor.scout_21_temperature_probe replayed against the real command sequence
reproduces the 88°F → 130.3°F run to within ~1°F (model peak 129.5°F).

- C = 1000 W·min/°F: ~4 kW into a stagnant loop climbs ~3°F/min.
- K_AMB = 30 W/°F to 80°F (piping, boiler jacket): a stopped 125°F loop
  falls ~1.3°F/min; the floor alone (1.5 kW) holds a stagnant loop near
  130°F, which is why an all-idle loop needs the demand shutoff.
- UA_ZONE = 80 W/°F to 70°F rooms, scaled by ``calling`` (0..1, the share of
  zone flow open). All zones open: 104°F takes ~3.4 kW; half open ~2.1 kW.
- Heat path: what the miner draws reaches the loop after ``dead_s`` of dead
  time and a first-order lag ``tau_s``. The fit says 30 s + 60 s (the real
  loop answered a cut within 2-4 min). LAG_12MIN (3 + 9 min) is the
  pessimistic variant the controller's horizon is sized for.
- Every limit change restarts btminer: 0 W for RESTART_S, then the new limit.
- The probe reports once a minute, quantised to 0.1125°F like the Scout.

The real "10-15 min lag" seen in the history is mostly the zones satisfying
and the loop going stagnant with ~4 kW still running, not transport delay:
``calling`` falling to 0 multiplies the rise rate ~2.5×.
"""
from __future__ import annotations

import math

C = 1000.0
K_AMB, T_AMB = 30.0, 80.0
UA_ZONE, T_ROOM = 80.0, 70.0
LAG_FITTED = (30.0, 60.0)  # (dead_s, tau_s)
LAG_12MIN = (180.0, 540.0)
RESTART_S = 60.0
PROBE_PERIOD_S, PROBE_STEP = 60.0, 0.1125
POLL_S = 15.0


class LoopPlant:
    """Wraps a test Rig: fakes the miner's commands and drives the supply probe."""

    def __init__(
        self, rig, supply: float, limit: int, mining: bool, calling=lambda plant: 1.0, lag=LAG_FITTED
    ):
        self.rig = rig
        self.dead_s, self.tau_s = lag
        self.t = rig.clock.t
        self.temp = supply
        self.limit = int(limit)
        self.hashing = mining
        self.calling = calling
        self.q_in = float(limit) if mining else 0.0
        self.power_hist: list[tuple[float, float]] = [(self.t - self.dead_s - 1, self.q_in)]
        self.power_from = self.t  # draw is 0 until then (boot / restart)
        self.uptime = 86400.0 if mining else 0.0
        self.probe = supply
        self.probe_at = self.t
        self.max_temp = supply
        self.trace: list[tuple[float, float, int, bool]] = []
        self.commands: list[tuple[float, str, int | None]] = []
        self.zones_on = 1.0
        api = rig.coord.api
        api.set_power_limit, api.power_on, api.power_off = self._set_limit, self._power_on, self._power_off
        self._publish()

    # --- fake firmware ----------------------------------------------------------
    async def _set_limit(self, watts):
        self.commands.append((self.rig.clock.t, "set_power_limit", int(watts)))
        self.rig.coord.api.calls.append(("set_power_limit", int(watts)))
        self.limit = int(watts)
        self._restart()

    async def _power_on(self):
        self.commands.append((self.rig.clock.t, "power_on", None))
        self.rig.coord.api.calls.append(("power_on", None))
        self.hashing = True
        self._restart()

    async def _power_off(self):
        self.commands.append((self.rig.clock.t, "power_off", None))
        self.rig.coord.api.calls.append(("power_off", None))
        self.hashing = False
        self._set_draw()

    def _restart(self):
        self.power_from = self.rig.clock.t + RESTART_S
        self.uptime = 0.0
        self._set_draw()

    def draw(self, t: float) -> float:
        return float(self.limit) if self.hashing and t >= self.power_from else 0.0

    def _set_draw(self):
        self.power_hist.append((self.rig.clock.t, self.draw(self.rig.clock.t)))

    def _delayed_draw(self, t: float) -> float:
        when = t - self.dead_s
        value = self.power_hist[0][1]
        for ts, w in self.power_hist:
            if ts <= when:
                value = w
            else:
                break
        return value

    # --- physics ------------------------------------------------------------------
    def advance(self, dt: float = POLL_S, sub: float = 5.0):
        steps = max(1, int(dt / sub))
        h = dt / steps
        for _ in range(steps):
            self.t += h
            if self.hashing and self.power_from <= self.t < self.power_from + h + 1e-9:
                self.power_hist.append((self.power_from, float(self.limit)))
            target = self._delayed_draw(self.t)
            self.q_in += (target - self.q_in) * (1 - math.exp(-h / self.tau_s))
            self.zones_on = float(self.calling(self))
            loss = K_AMB * (self.temp - T_AMB) + self.zones_on * UA_ZONE * (self.temp - T_ROOM)
            self.temp += (self.q_in - loss) / C * (h / 60.0)
        self.max_temp = max(self.max_temp, self.temp)
        if self.t - self.probe_at >= PROBE_PERIOD_S - 1e-6:
            self.probe = round(self.temp / PROBE_STEP) * PROBE_STEP
            self.probe_at = self.t
        if self.hashing:
            self.uptime += dt
        self._publish()
        self.trace.append((self.t, self.temp, self.limit, self.hashing))

    def _publish(self):
        rig = self.rig
        booting = self.hashing and rig.clock.t < self.power_from - RESTART_S + POLL_S
        hashing = self.hashing and not booting
        rig.mining(hashing, limit=self.limit if hashing else 0)
        rig.coord.data["uptime"] = self.uptime if self.hashing else 0
        rig.set_supply(round(self.probe, 4))

    async def run(self, minutes: float, until=None):
        end = self.rig.clock.t + minutes * 60
        while self.rig.clock.t < end:
            self.advance(POLL_S)
            self.rig.set_thermostats("heating" if self.zones_on else "idle")
            await self.rig.tick(POLL_S)
            if until is not None and until(self):
                break

    def limits(self):
        return [w for _, c, w in self.commands if c == "set_power_limit"]
