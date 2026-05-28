import type { ReplayEvent } from "../replay-core/types/event";
import type { ReplayLog } from "../replay-core/types/replay";

const ROLLING_EVENT_TYPES = new Set([
  "rolling_horizon_window_started",
  "rolling_horizon_candidate_collected",
  "rolling_horizon_dispatched",
  "rolling_horizon_task_skipped",
  "rolling_horizon_task_requeued",
  "rolling_horizon_task_started",
  "rolling_horizon_task_completed",
]);

type RollingTaskStatus = "pool" | "requeued" | "dispatched" | "started" | "completed" | "skipped";

export interface RollingTaskPoolEntry {
  windowIndex: number;
  rowKey: string;
  opportunityId: string;
  taskId?: string;
  taskCode: string;
  taskType?: string;
  baseRank?: number;
  effectiveRank?: number;
  waitedWindows?: number;
  target: string;
  status: RollingTaskStatus;
  workerIds: string[];
  roleOwnerAgentId?: string;
  allowedWorkerIds: string[];
  rolePolicy?: string;
  assignedWorkerId?: string;
  skipReason?: string;
  collectedAt: number;
  updatedAt: number;
  plannedOrder?: number;
}

export interface RollingWindowSummary {
  windowIndex: number;
  startedAt: number;
  endedAt?: number;
  entryCount: number;
  dispatchedCount: number;
  skippedCount: number;
}

export interface RollingTaskPoolModel {
  focusWindowIndex?: number;
  focusWindow?: RollingWindowSummary;
  entries: RollingTaskPoolEntry[];
  windows: RollingWindowSummary[];
  recentEvents: ReplayEvent[];
}

interface MutableEntry {
  windowIndex: number;
  rowKey: string;
  opportunityId: string;
  taskId?: string;
  taskCode: string;
  taskType?: string;
  baseRank?: number;
  effectiveRank?: number;
  waitedWindows?: number;
  target: string;
  status: RollingTaskStatus;
  workerIds: Set<string>;
  roleOwnerAgentId?: string;
  allowedWorkerIds: Set<string>;
  rolePolicy?: string;
  assignedWorkerId?: string;
  skipReason?: string;
  collectedAt: number;
  updatedAt: number;
  plannedOrder?: number;
}

interface MutableWindow {
  windowIndex: number;
  startedAt: number;
  endedAt?: number;
  entries: Map<string, MutableEntry>;
}

function asNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function asText(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const text = value.trim();
  return text || undefined;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function opportunityId(event: ReplayEvent): string {
  return asText(event.payload.opportunity_id) ?? asText(event.entity_refs.primary) ?? event.event_id;
}

function taskIdFromInstance(instanceId: string | undefined): string | undefined {
  if (!instanceId) return undefined;
  const [taskId] = instanceId.split(":");
  return taskId.trim() || undefined;
}

function lifecycleTaskId(event: ReplayEvent): string | undefined {
  return asText(event.payload.task_id) ?? taskIdFromInstance(asText(event.payload.instance_id)) ?? asText(event.entity_refs.primary);
}

function taskRowKey(event: ReplayEvent): string {
  return lifecycleTaskId(event) ?? opportunityId(event);
}

function isTaskOpportunityEvent(event: ReplayEvent): boolean {
  return opportunityId(event).startsWith("RHOPP-");
}

function windowIndex(event: ReplayEvent): number {
  return asNumber(event.payload.window_index) ?? 0;
}

function compactTarget(payload: Record<string, unknown>): string {
  const signature = asRecord(payload.rolling_task_signature ?? payload.task_signature);
  const transferKind = typeof signature.transfer_kind === "string" ? signature.transfer_kind.trim() : "";
  const targetId = typeof signature.target_id === "string" ? signature.target_id.trim() : "";
  if (
    transferKind === "material_supply" &&
    targetId &&
    !asText(signature.source_slot_id) &&
    !asText(signature.transfer_item_id)
  ) {
    return `${targetId} / any material from Warehouse`;
  }
  const itemIds = Array.isArray(signature.item_ids) ? signature.item_ids.map((item) => String(item)).filter(Boolean) : [];
  const parts = [
    signature.target_id,
    signature.machine_id,
    signature.source_slot_id,
    signature.transfer_item_id,
    signature.item_id,
    itemIds.length ? itemIds.join(",") : undefined,
    signature.destination,
  ]
    .map((part) => (typeof part === "string" ? part.trim() : ""))
    .filter(Boolean);
  return parts.length ? parts.slice(0, 3).join(" / ") : "-";
}

function makeEntry(event: ReplayEvent, status: RollingTaskStatus): MutableEntry {
  const payload = event.payload;
  const workerId = asText(payload.worker_id);
  const assignedWorkerId = asText(payload.assigned_worker_id);
  const roleOwnerAgentId = asText(payload.role_owner_agent_id);
  const rolePolicy = asText(payload.role_policy);
  const allowedWorkerIds = Array.isArray(payload.allowed_worker_ids)
    ? payload.allowed_worker_ids.map((item) => String(item).trim()).filter(Boolean)
    : [];
  const workerIds = new Set<string>();
  if (workerId) workerIds.add(workerId);
  if (assignedWorkerId) workerIds.add(assignedWorkerId);
  if (roleOwnerAgentId) workerIds.add(roleOwnerAgentId);
  return {
    windowIndex: windowIndex(event),
    rowKey: taskRowKey(event),
    opportunityId: opportunityId(event),
    taskId: asText(payload.task_id),
    taskCode: asText(payload.task_code) ?? "-",
    taskType: asText(payload.task_type),
    baseRank: asNumber(payload.base_priority_rank),
    effectiveRank: asNumber(payload.effective_priority_rank),
    waitedWindows: asNumber(payload.waited_window_count),
    target: compactTarget(payload),
    status,
    workerIds,
    roleOwnerAgentId,
    allowedWorkerIds: new Set(allowedWorkerIds),
    rolePolicy,
    assignedWorkerId,
    skipReason: asText(payload.reason),
    collectedAt: event.timestamp,
    updatedAt: event.timestamp,
  };
}

function toSummary(window: MutableWindow): RollingWindowSummary {
  const entries = [...window.entries.values()];
  return {
    windowIndex: window.windowIndex,
    startedAt: window.startedAt,
    endedAt: window.endedAt,
    entryCount: entries.length,
    dispatchedCount: entries.filter((entry) => entry.status === "dispatched").length,
    skippedCount: entries.filter((entry) => entry.status === "skipped").length,
  };
}

export function isRollingHorizonReplay(log: ReplayLog | null): boolean {
  if (!log) return false;
  const mode = String(log.metadata.decision_mode ?? "").trim().toLowerCase();
  return (
    mode === "rolling_horizon_aging_priority" ||
    mode === "rolling_horizon_dedicated_roles" ||
    mode === "rolling_horizon_fixed_priority" ||
    log.events.some((event) => ROLLING_EVENT_TYPES.has(event.event_type))
  );
}

export function buildRollingTaskPoolModel(events: ReplayEvent[], currentTime: number): RollingTaskPoolModel {
  const windows = new Map<number, MutableWindow>();
  const entriesByRowKey = new Map<string, MutableEntry>();
  const entriesByTaskId = new Map<string, MutableEntry>();
  let lastWindowIndex: number | undefined;
  const recentEvents: ReplayEvent[] = [];

  function getWindow(index: number, timestamp: number): MutableWindow {
    const existing = windows.get(index);
    if (existing) return existing;
    const created: MutableWindow = {
      windowIndex: index,
      startedAt: timestamp,
      entries: new Map(),
    };
    windows.set(index, created);
    return created;
  }

  for (const event of events) {
    if (event.timestamp > currentTime || !ROLLING_EVENT_TYPES.has(event.event_type)) continue;
    recentEvents.push(event);
    if (recentEvents.length > 6) recentEvents.shift();

    const index = windowIndex(event);
    lastWindowIndex = Math.max(lastWindowIndex ?? index, index);
    const currentWindow = getWindow(index, event.timestamp);

    if (event.event_type === "rolling_horizon_window_started") {
      currentWindow.startedAt = asNumber(event.payload.window_start_min) ?? event.timestamp;
      currentWindow.endedAt = asNumber(event.payload.window_end_min);
      continue;
    }

    if (event.event_type === "rolling_horizon_task_started" || event.event_type === "rolling_horizon_task_completed") {
      const taskId = lifecycleTaskId(event);
      const entry = taskId ? entriesByTaskId.get(taskId) : undefined;
      if (entry) {
        entry.status = event.event_type === "rolling_horizon_task_started" ? "started" : "completed";
        entry.updatedAt = event.timestamp;
        entry.assignedWorkerId = asText(event.payload.worker_id) ?? asText(event.entity_refs.target) ?? entry.assignedWorkerId;
        const workerId = asText(event.payload.worker_id) ?? asText(event.entity_refs.target);
        if (workerId) entry.workerIds.add(workerId);
      }
      continue;
    }

    if (!isTaskOpportunityEvent(event)) {
      continue;
    }

    const id = taskRowKey(event);
    const existing = currentWindow.entries.get(id);
    const existingGlobal = entriesByRowKey.get(id);
    const status: RollingTaskStatus =
      event.event_type === "rolling_horizon_dispatched"
        ? "dispatched"
        : event.event_type === "rolling_horizon_task_skipped"
          ? "skipped"
          : event.event_type === "rolling_horizon_task_requeued"
            ? "requeued"
            : "pool";
    const entry = existing ?? makeEntry(event, status);
    if (existing) {
      entry.updatedAt = event.timestamp;
      entry.windowIndex = index;
      if (event.event_type === "rolling_horizon_dispatched") entry.status = "dispatched";
      if (event.event_type === "rolling_horizon_task_skipped") entry.status = "skipped";
      if (event.event_type === "rolling_horizon_task_requeued") entry.status = "requeued";
      entry.taskId = asText(event.payload.task_id) ?? entry.taskId;
      entry.assignedWorkerId = asText(event.payload.assigned_worker_id) ?? entry.assignedWorkerId;
      entry.skipReason = asText(event.payload.reason) ?? entry.skipReason;
      entry.baseRank = asNumber(event.payload.base_priority_rank) ?? entry.baseRank;
      entry.effectiveRank = asNumber(event.payload.effective_priority_rank) ?? entry.effectiveRank;
      entry.waitedWindows = asNumber(event.payload.waited_window_count) ?? entry.waitedWindows;
      const workerId = asText(event.payload.worker_id);
      const assignedWorkerId = asText(event.payload.assigned_worker_id);
      if (workerId) entry.workerIds.add(workerId);
      if (assignedWorkerId) entry.workerIds.add(assignedWorkerId);
      const roleOwnerAgentId = asText(event.payload.role_owner_agent_id);
      if (roleOwnerAgentId) {
        entry.roleOwnerAgentId = roleOwnerAgentId;
        entry.workerIds.add(roleOwnerAgentId);
      }
      const rolePolicy = asText(event.payload.role_policy);
      if (rolePolicy) entry.rolePolicy = rolePolicy;
      const allowedWorkerIds = Array.isArray(event.payload.allowed_worker_ids)
        ? event.payload.allowed_worker_ids.map((item) => String(item).trim()).filter(Boolean)
        : [];
      for (const id of allowedWorkerIds) entry.allowedWorkerIds.add(id);
    }
    currentWindow.entries.set(id, entry);
    if (entry.taskId) entriesByTaskId.set(entry.taskId, entry);

    const globalEntry = existingGlobal ?? makeEntry(event, status);
    if (existingGlobal) {
      if (status !== "pool") {
        globalEntry.status = status;
        globalEntry.windowIndex = index;
        globalEntry.updatedAt = event.timestamp;
      } else if (globalEntry.status === "pool") {
        globalEntry.windowIndex = index;
      }
      globalEntry.taskId = asText(event.payload.task_id) ?? globalEntry.taskId;
      globalEntry.assignedWorkerId = asText(event.payload.assigned_worker_id) ?? globalEntry.assignedWorkerId;
      globalEntry.skipReason = asText(event.payload.reason) ?? globalEntry.skipReason;
      globalEntry.baseRank = asNumber(event.payload.base_priority_rank) ?? globalEntry.baseRank;
      globalEntry.effectiveRank = asNumber(event.payload.effective_priority_rank) ?? globalEntry.effectiveRank;
      globalEntry.waitedWindows = asNumber(event.payload.waited_window_count) ?? globalEntry.waitedWindows;
      const workerId = asText(event.payload.worker_id);
      const assignedWorkerId = asText(event.payload.assigned_worker_id);
      if (workerId) globalEntry.workerIds.add(workerId);
      if (assignedWorkerId) globalEntry.workerIds.add(assignedWorkerId);
      const roleOwnerAgentId = asText(event.payload.role_owner_agent_id);
      if (roleOwnerAgentId) {
        globalEntry.roleOwnerAgentId = roleOwnerAgentId;
        globalEntry.workerIds.add(roleOwnerAgentId);
      }
      const rolePolicy = asText(event.payload.role_policy);
      if (rolePolicy) globalEntry.rolePolicy = rolePolicy;
      const allowedWorkerIds = Array.isArray(event.payload.allowed_worker_ids)
        ? event.payload.allowed_worker_ids.map((item) => String(item).trim()).filter(Boolean)
        : [];
      for (const id of allowedWorkerIds) globalEntry.allowedWorkerIds.add(id);
    }
    entriesByRowKey.set(id, globalEntry);
    if (globalEntry.taskId) entriesByTaskId.set(globalEntry.taskId, globalEntry);
  }

  const summaries = [...windows.values()].sort((left, right) => left.windowIndex - right.windowIndex).map(toSummary);
  const latestNonEmptyWindow = [...summaries].reverse().find((window) => window.entryCount > 0);
  const timeActiveWindow = summaries.find((window) => currentTime >= window.startedAt && currentTime < (window.endedAt ?? Number.POSITIVE_INFINITY));
  const activeWindow =
    (timeActiveWindow?.entryCount ? timeActiveWindow : undefined) ??
    latestNonEmptyWindow ??
    timeActiveWindow ??
    summaries[summaries.length - 1];
  const focusWindowIndex = activeWindow?.windowIndex ?? lastWindowIndex;
  const visibleWindowIndexes = new Set<number>();
  if (focusWindowIndex !== undefined) {
    visibleWindowIndexes.add(focusWindowIndex);
    if (focusWindowIndex > 0) visibleWindowIndexes.add(focusWindowIndex - 1);
  }
  const entries = [...entriesByRowKey.values()]
    .filter(
      (entry) =>
        entry.status !== "completed" &&
        (visibleWindowIndexes.has(entry.windowIndex) || entry.status === "pool" || entry.status === "requeued" || entry.status === "dispatched" || entry.status === "started"),
    )
    .sort(
      (left, right) =>
        left.windowIndex - right.windowIndex ||
        (left.effectiveRank ?? Number.POSITIVE_INFINITY) - (right.effectiveRank ?? Number.POSITIVE_INFINITY) ||
        left.taskCode.localeCompare(right.taskCode),
    );

  const queuedByWorker = new Map<string, MutableEntry[]>();
  for (const entry of entries) {
    if (entry.status !== "dispatched" || !entry.assignedWorkerId) continue;
    const bucket = queuedByWorker.get(entry.assignedWorkerId) ?? [];
    bucket.push(entry);
    queuedByWorker.set(entry.assignedWorkerId, bucket);
  }
  for (const bucket of queuedByWorker.values()) {
    bucket.sort((left, right) => left.updatedAt - right.updatedAt || left.collectedAt - right.collectedAt || left.taskCode.localeCompare(right.taskCode));
    bucket.forEach((entry, index) => {
      entry.plannedOrder = index + 1;
    });
  }
  for (const entry of entries) {
    if (entry.status === "started") entry.plannedOrder = 0;
  }

  const outputEntries = entries.map((entry) => ({
      windowIndex: entry.windowIndex,
      rowKey: entry.rowKey,
      opportunityId: entry.opportunityId,
      taskId: entry.taskId,
      taskCode: entry.taskCode,
      taskType: entry.taskType,
      baseRank: entry.baseRank,
      effectiveRank: entry.effectiveRank,
      waitedWindows: entry.waitedWindows,
      target: entry.target,
      status: entry.status,
      workerIds: [...entry.workerIds].sort(),
      roleOwnerAgentId: entry.roleOwnerAgentId,
      allowedWorkerIds: [...entry.allowedWorkerIds].sort(),
      rolePolicy: entry.rolePolicy,
      assignedWorkerId: entry.assignedWorkerId,
      skipReason: entry.skipReason,
      collectedAt: entry.collectedAt,
      updatedAt: entry.updatedAt,
      plannedOrder: entry.plannedOrder,
    }));

  return {
    focusWindowIndex,
    focusWindow: activeWindow,
    entries: outputEntries,
    windows: summaries,
    recentEvents,
  };
}

function formatTime(value: number | undefined, unit: string): string {
  if (value === undefined || !Number.isFinite(value)) return "-";
  return `${value.toFixed(1)} ${unit === "seconds" ? "s" : "min"}`;
}

export function RollingTaskPoolPanel({
  events,
  currentTime,
  totalDuration,
  timeUnit,
  decisionMode,
}: {
  events: ReplayEvent[];
  currentTime: number;
  totalDuration: number;
  timeUnit: string;
  decisionMode?: string;
}) {
  const model = buildRollingTaskPoolModel(events, currentTime);
  const focus = model.focusWindow;
  void decisionMode;

  return (
    <section className="rolling-pool-panel" aria-label="Rolling horizon task pool">
      <div className="rolling-pool-header">
        <div>
          <span className="rolling-pool-kicker">Rolling Horizon</span>
          <strong>Task Pool</strong>
        </div>
        <div className="rolling-pool-stats">
          <span>Window {focus?.windowIndex ?? "-"}</span>
          <span>{model.entries.length} visible</span>
          <span>{focus?.dispatchedCount ?? 0} dispatched</span>
          <span>{focus?.skippedCount ?? 0} skipped</span>
        </div>
      </div>

      <div className="rolling-window-strip" aria-label="Rolling horizon windows">
        {model.windows.length === 0 ? (
          <div className="rolling-empty">No rolling horizon events yet.</div>
        ) : (
          model.windows.map((window) => {
            const left = totalDuration > 0 ? Math.max(0, (window.startedAt / totalDuration) * 100) : 0;
            const right = totalDuration > 0 ? Math.max(0, (((window.endedAt ?? currentTime) - window.startedAt) / totalDuration) * 100) : 100;
            const active = window.windowIndex === model.focusWindowIndex;
            return (
              <div
                key={window.windowIndex}
                className={`rolling-window-chip${active ? " active" : ""}`}
                style={{ left: `${left}%`, width: `${Math.max(2, right)}%` }}
                title={`Window ${window.windowIndex}: ${window.entryCount} pooled, ${window.dispatchedCount} dispatched`}
              >
                {window.windowIndex}
              </div>
            );
          })
        )}
      </div>

      <div className="rolling-pool-body">
        <div className="rolling-table-wrap">
          <table className="rolling-pool-table">
            <thead>
              <tr>
                <th title="Window">Win</th>
                <th title="Opportunity ID">ID</th>
                <th title="First Seen">First</th>
                <th title="Status Time">Updated</th>
                <th title="Task Code">Task</th>
                <th>Target</th>
                <th title="Execution order in the assigned worker queue. Running task is 0.">Seq</th>
                <th title="Assigned Worker">Worker</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {model.entries.length === 0 ? (
                <tr>
                  <td colSpan={9}>No visible rolling horizon task opportunities yet.</td>
                </tr>
              ) : (
                model.entries.map((entry) => (
                  <tr key={entry.rowKey}>
                    <td>{entry.windowIndex}</td>
                    <td title={entry.opportunityId}>{entry.taskId || entry.opportunityId.replace(/^RHOPP-/, "")}</td>
                    <td>{formatTime(entry.collectedAt, timeUnit)}</td>
                    <td>{formatTime(entry.updatedAt, timeUnit)}</td>
                    <td>{entry.taskCode}</td>
                    <td>{entry.target}</td>
                    <td title={entry.effectiveRank !== undefined ? `rank ${entry.effectiveRank}, base ${entry.baseRank ?? "-"}, waited ${entry.waitedWindows ?? 0}` : undefined}>
                      {entry.plannedOrder ?? "-"}
                    </td>
                    <td>{entry.status === "dispatched" || entry.status === "started" ? entry.assignedWorkerId ?? "-" : "-"}</td>
                    <td>
                      <span className={`rolling-status ${entry.status}`}>{entry.status}</span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
