from __future__ import annotations

from typing import TYPE_CHECKING

import simpy

from manufacturing_sim.simulation.scenarios.manufacturing.entities import MachineState

if TYPE_CHECKING:
    from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


def machine_lifecycle(env: simpy.Environment, world: ManufacturingWorld, machine_id: str):
    machine = world.machines[machine_id]
    while True:
        if machine.broken:
            machine.state = MachineState.BROKEN
            yield env.timeout(1)
            continue

        if machine.output_intermediate is not None:
            machine.state = MachineState.DONE_WAIT_UNLOAD
            yield env.timeout(1)
            continue

        needs_intermediate = world._station_requires_intermediate(machine.station)
        if machine.input_material is None or (needs_intermediate and machine.input_intermediate is None):
            machine.state = MachineState.WAIT_INPUT
            yield env.timeout(1)
            continue

        cycle_id = world.start_machine_cycle(machine)
        start_t = env.now
        machine.active_process = env.active_process
        try:
            yield env.timeout(machine.process_time_min)
        except simpy.Interrupt as intr:
            machine.total_processing_min += max(0.0, env.now - start_t)
            world.abort_machine_cycle(machine, cycle_id, str(intr.cause))
            continue
        machine.total_processing_min += machine.process_time_min
        world.complete_machine_cycle(machine, cycle_id)


def machine_failure_monitor(env: simpy.Environment, world: ManufacturingWorld, machine_id: str):
    machine = world.machines[machine_id]
    while True:
        lam = world.machine_failure_lambda(machine)
        if lam <= 0.0:
            yield env.timeout(60)
            continue
        ttf = max(1.0, world.rng.expovariate(lam))
        yield env.timeout(ttf)
        world.break_machine(machine, reason="stochastic")


def agent_work_loop(env: simpy.Environment, world: ManufacturingWorld, agent_id: str):
    agent = world.agents[agent_id]
    while True:
        if world.terminated:
            return
        agent.process_ref = env.active_process
        try:
            if agent.discharged:
                yield env.timeout(1)
                continue
            if agent.awaiting_battery_from is not None:
                # Assisted battery swap in progress: receiver must stay paused.
                yield env.timeout(1)
                continue

            resumed_task = False
            if agent.suspended_task is not None:
                task = agent.suspended_task
                resumed_task = True
            else:
                task = world.select_task_for_agent(agent)
            if task is None:
                yield env.timeout(1)
                continue

            start_t = env.now
            world.start_agent_task(agent, task, start_t)
            status = "completed"
            reason = ""
            try:
                completed = yield from world.execute_task(agent, task)
                if not completed:
                    status = "skipped"
                    reason = "precondition_failed"
                elif resumed_task and agent.suspended_task is task:
                    agent.suspended_task = None
            except simpy.Interrupt as intr:
                status = "interrupted"
                reason = str(intr.cause)
                world.handle_task_interruption(agent, task, reason)
            finally:
                if not world.logger.closed:
                    world.finish_agent_task(agent, task, start_t, status, reason)

            if resumed_task and status == "skipped" and not agent.discharged and agent.suspended_task is task:
                # Suspended task became invalid after recharge (e.g. machine state changed):
                # release it to prevent infinite retry loops.
                agent.suspended_task = None

            # Prevent zero-time tight loops when a task is repeatedly skipped/interrupted
            # due to stale preconditions selected by parallel agents.
            if status != "completed":
                yield env.timeout(0.5)
        except simpy.Interrupt:
            # Battery depletion can interrupt while idle/backoff timeout.
            continue


def agent_battery_monitor(env: simpy.Environment, world: ManufacturingWorld, agent_id: str):
    agent = world.agents[agent_id]
    # Guard against floating-point residue (e.g. 2e-13 min) that can cause
    # same-timestamp timeout churn in SimPy.
    eps = 1e-6
    while True:
        if world.terminated:
            return
        if agent.discharged:
            yield env.timeout(1)
            continue
        if getattr(agent, "battery_swap_critical", False):
            # Keep battery-delivery handover atomic once it has started.
            yield env.timeout(0.5)
            continue

        remaining = world.battery_remaining(agent)
        world._emit_low_battery_alert_if_needed(agent)
        if remaining <= eps:
            world.discharge_agent(agent, reason="battery_depleted")
            yield env.timeout(0)
            continue

        yield env.timeout(max(remaining, eps))
        if not agent.discharged and world.battery_remaining(agent) <= eps:
            world.discharge_agent(agent, reason="battery_depleted")


def snapshot_loop(env: simpy.Environment, world: ManufacturingWorld):
    while True:
        if world.terminated:
            return
        world.capture_snapshot()
        yield env.timeout(world.snapshot_interval)
