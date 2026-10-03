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
            # ``broken`` remains true while repair is active, but the observable
            # machine state must stay UNDER_REPAIR until the team leaves or the
            # repair completes. Reasserting BROKEN here made replay state flicker
            # and split repair time into misleading broken-wait intervals.
            target_state = (
                MachineState.UNDER_REPAIR
                if world._repair_team_size(machine) > 0
                else MachineState.BROKEN
            )
            if machine.state != target_state:
                world._set_machine_state(
                    machine,
                    target_state,
                    reason="repair_active" if target_state == MachineState.UNDER_REPAIR else "broken_wait",
                )
            yield env.timeout(1)
            continue

        # A reserved or active PM owns the machine even when its inputs are ready.
        # Do not overwrite UNDER_PM with WAIT_INPUT or start a cycle behind its owner.
        if machine.pm_owner is not None or machine.state == MachineState.UNDER_PM:
            yield env.timeout(1)
            continue

        if machine.output_intermediate is not None:
            world._set_machine_state(machine, MachineState.DONE_WAIT_UNLOAD, reason="output_waiting_unload")
            yield env.timeout(1)
            continue

        needs_intermediate = world._station_requires_intermediate(machine.station)
        if machine.input_material is None or (needs_intermediate and machine.input_intermediate is None):
            world._set_machine_state(machine, MachineState.WAIT_INPUT, reason="waiting_input")
            yield env.timeout(1)
            continue

        if not bool(getattr(machine, "setup_ready", False)):
            world._set_machine_state(machine, MachineState.WAIT_INPUT, reason="waiting_setup")
            yield env.timeout(1)
            continue

        cycle_id = world.start_machine_cycle(machine)
        process_duration = max(0.0, float(machine.cycle_remaining_process_min))
        start_t = env.now
        machine.active_process = env.active_process
        if world.machine_failure_time_basis == "active_processing":
            cycle_elapsed = 0.0
            interrupted = False
            try:
                while cycle_elapsed < process_duration - 1e-9:
                    remaining = process_duration - cycle_elapsed
                    segment = world.machine_processing_segment_limit(machine, remaining)
                    segment_start = env.now
                    try:
                        yield env.timeout(segment)
                    except simpy.Interrupt as intr:
                        elapsed_min = max(0.0, env.now - segment_start)
                        world.record_machine_processing_exposure(machine, elapsed_min)
                        cycle_elapsed += elapsed_min
                        world.abort_machine_cycle(
                            machine,
                            cycle_id,
                            str(intr.cause),
                            elapsed_min=cycle_elapsed,
                        )
                        interrupted = True
                        break
                    world.record_machine_processing_exposure(machine, segment)
                    cycle_elapsed += segment
                    if world.machine_failure_threshold_reached(machine):
                        world.log_machine_failure_processing_threshold(machine)
                        world.break_machine(
                            machine,
                            reason="stochastic_processing_exposure",
                            interrupt_active_process=False,
                        )
                        world.abort_machine_cycle(
                            machine,
                            cycle_id,
                            "machine_breakdown",
                            elapsed_min=cycle_elapsed,
                        )
                        interrupted = True
                        break
            finally:
                machine.active_process = None
            if interrupted:
                continue
            world.complete_machine_cycle(machine, cycle_id)
            continue
        try:
            yield env.timeout(process_duration)
        except simpy.Interrupt as intr:
            elapsed_min = max(0.0, env.now - start_t)
            machine.total_processing_min += elapsed_min
            world.abort_machine_cycle(machine, cycle_id, str(intr.cause), elapsed_min=elapsed_min)
            continue
        machine.total_processing_min += process_duration
        world.complete_machine_cycle(machine, cycle_id)


def machine_failure_monitor(env: simpy.Environment, world: ManufacturingWorld, machine_id: str):
    machine = world.machines[machine_id]
    if world.machine_failure_time_basis == "active_processing":
        return
    while True:
        lam = world.machine_failure_lambda(machine)
        if lam <= 0.0:
            yield env.timeout(60)
            continue
        ttf = world.sample_machine_failure_delay(machine, lam)
        yield env.timeout(ttf)
        world.break_machine(machine, reason="stochastic")


def worker_work_loop(env: simpy.Environment, world: ManufacturingWorld, worker_id: str):
    agent_id = worker_id
    agent = world.agents[agent_id]
    while True:
        if world.terminated:
            return
        agent.process_ref = env.active_process
        try:
            if agent.discharged:
                recovery_task = world.mandatory_task_for_agent(agent)
                if recovery_task is None or recovery_task.task_type != "BATTERY_CHARGE":
                    yield env.timeout(1)
                    continue
                task = recovery_task
                resumed_task = False
            else:
                task = None
            if agent.awaiting_battery_from is not None:
                # Assisted battery swap in progress: receiver must stay paused.
                yield env.timeout(1)
                continue
            transport_session_for_worker = getattr(world, "_transport_session_for_worker", None)
            if callable(transport_session_for_worker) and transport_session_for_worker(agent) is not None:
                # Shared product transport is still active even when the helper's
                # HANDOVER_ITEM task has already synchronized with the carrier.
                yield env.timeout(0.1)
                continue

            resumed_task = False
            if task is None:
                if agent.suspended_task is not None:
                    task = agent.suspended_task
                    resumed_task = True
                else:
                    task = world.select_task_for_agent(agent)
            if task is None:
                if world._adp_active():
                    dispatch_event = world.adp_dispatch_event(agent.agent_id)
                    yield dispatch_event | world.termination_event
                elif world._rolling_horizon_active():
                    dispatch_event = world.rolling_horizon_dispatch_event(agent.agent_id)
                    yield dispatch_event | world.termination_event
                else:
                    yield env.timeout(1)
                continue

            start_t = env.now
            world.start_agent_task(agent, task, start_t)
            status = "completed"
            reason = ""
            try:
                assignment_min = float(getattr(getattr(world, "humanoid_runtime", None), "assignment_min_duration", 0.0) or 0.0)
                if assignment_min > 0.0:
                    yield env.timeout(assignment_min)
                completed = yield from world.execute_task(agent, task)
                if not completed:
                    status = "skipped"
                    reason = str(task.payload.pop("failure_reason", "") or "precondition_failed")
                elif resumed_task and agent.suspended_task is task:
                    agent.suspended_task = None
            except simpy.Interrupt as intr:
                status = "interrupted"
                reason = str(intr.cause)
                world.handle_task_interruption(agent, task, reason)

            # Keep the exclusive desk owner until the outgoing worker has
            # physically cleared its sole service tile. This also applies to
            # recoverable interruptions after a desk state transition (for
            # example, an item drop recovered during RELEASE). Suspended tasks
            # retain their position and owner because the same worker resumes.
            try:
                if not world.terminated and agent.suspended_task is not task:
                    yield from world.vacate_completed_task_service_tile(agent, task)
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


def agent_work_loop(env: simpy.Environment, world: ManufacturingWorld, agent_id: str):
    # Deprecated compatibility wrapper.
    yield from worker_work_loop(env, world, agent_id)


def worker_battery_monitor(env: simpy.Environment, world: ManufacturingWorld, worker_id: str):
    agent_id = worker_id
    agent = world.agents[agent_id]
    # Guard against floating-point residue (e.g. 2e-13 min) that can cause
    # same-timestamp timeout churn in SimPy.
    eps = 1e-6
    while True:
        if world.terminated:
            return
        if agent.charging_started_at is not None:
            world.battery_remaining(agent)
            yield env.timeout(world._battery_monitor_sleep_min(agent, eps))
            continue
        if agent.discharged:
            yield env.timeout(1)
            continue
        if getattr(agent, "battery_swap_critical", False):
            # Keep battery-delivery handover atomic once it has started.
            yield env.timeout(0.5)
            continue

        remaining = world.battery_remaining(agent)
        world._sync_humanoid_power_state(agent)
        world._emit_low_battery_alert_if_needed(agent)
        if remaining <= eps:
            if (
                world.battery_direct_charge_enabled
                and agent.current_task_type == "BATTERY_CHARGE"
                and world._worker_at_assigned_charging_dock(agent)
            ):
                yield env.timeout(eps)
                continue
            world.discharge_agent(agent, reason="battery_depleted")
            yield env.timeout(0)
            continue

        yield env.timeout(world._battery_monitor_sleep_min(agent, eps))
        if not agent.discharged and world.battery_remaining(agent) <= eps:
            world.discharge_agent(agent, reason="battery_depleted")


def agent_battery_monitor(env: simpy.Environment, world: ManufacturingWorld, agent_id: str):
    # Deprecated compatibility wrapper.
    yield from worker_battery_monitor(env, world, agent_id)


def snapshot_loop(env: simpy.Environment, world: ManufacturingWorld):
    while True:
        if world.terminated:
            return
        world.capture_snapshot()
        world.log_periodic_worker_state_observations()
        yield env.timeout(world.snapshot_interval)
