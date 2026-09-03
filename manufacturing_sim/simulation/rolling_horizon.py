from __future__ import annotations

from typing import Any, Protocol

import simpy
from simpy.events import NORMAL, Event


POST_STATE_PRIORITY = NORMAL + 1


class PostStateTimeout(Event):
    """Timeout processed after normal domain events at the same simulation time."""

    def __init__(self, env: simpy.Environment, delay: float, value: Any = None) -> None:
        if delay < 0:
            raise ValueError(f"Negative delay {delay}")
        # Mirror SimPy Timeout initialization while using a lower scheduling
        # priority. Normal machine/task events stamped at the boundary settle
        # before the rolling-horizon snapshot is taken.
        self.env = env
        self.callbacks = []
        self._value = value
        self._delay = delay
        self._ok = True
        env.schedule(self, POST_STATE_PRIORITY, delay)


class StrictPeriodicRollingHorizonWorld(Protocol):
    rolling_horizon_enabled: bool
    rolling_horizon_window_min: float
    terminated: bool

    def _rolling_horizon_initialize_strict_periodic(self) -> None: ...

    def _rolling_horizon_process_strict_boundary(self, scheduled_boundary_min: float) -> None: ...

    def _rolling_horizon_simulation_limit_min(self) -> float: ...


def strict_periodic_rolling_horizon_loop(
    env: simpy.Environment,
    world: StrictPeriodicRollingHorizonWorld,
):
    """Drive rolling-horizon dispatch at exact, worker-independent boundaries."""

    if not world.rolling_horizon_enabled:
        return

    world._rolling_horizon_initialize_strict_periodic()
    window_min = max(1e-9, float(world.rolling_horizon_window_min))
    next_boundary = window_min

    while not world.terminated:
        delay = max(0.0, next_boundary - float(env.now))
        yield PostStateTimeout(env, delay, next_boundary)
        if world.terminated:
            return

        simulation_limit = float(world._rolling_horizon_simulation_limit_min())
        if next_boundary >= simulation_limit - 1e-9:
            return

        world._rolling_horizon_process_strict_boundary(next_boundary)
        next_boundary += window_min
