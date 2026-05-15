import type { BaseEntityState } from "../replay-core/types/entity";
import type { ManifestRun } from "../routes";
import { cargoItemId, childTaskCode, humanoidStateValue, primitiveCode, taskCode } from "../scene/entityVisuals";
import { isMotionActive, motionPathPoints } from "../scene/coordinates";

interface EntityInspectorProps {
  entity?: BaseEntityState;
  currentTime: number;
  selectedRun?: ManifestRun;
}

function valueOrDash(value: unknown): string {
  if (value === undefined || value === null || value === "") return "-";
  return String(value);
}

function motionPathLabel(entity: BaseEntityState, currentTime: number): string {
  const motion = entity.attributes.motion;
  if (!isMotionActive(motion, currentTime)) return "0 tiles";
  return `${Math.max(0, motionPathPoints(motion).length)} tiles`;
}

function trafficConflictIsActive(conflict: Record<string, unknown>, currentTime: number): boolean {
  const timeWindow = conflict.time_window;
  if (!timeWindow || typeof timeWindow !== "object") return true;
  const payload = timeWindow as Record<string, unknown>;
  const startedAt = Number(payload.started_at);
  const endedAt = Number(payload.ended_at);
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt)) return true;
  return currentTime >= startedAt && currentTime <= endedAt;
}

function trafficConflict(entity: BaseEntityState, currentTime: number): string {
  const conflict = entity.attributes.last_traffic_conflict;
  if (!conflict || typeof conflict !== "object") return "-";
  const payload = conflict as Record<string, unknown>;
  if (!trafficConflictIsActive(payload, currentTime)) return "-";
  const type = valueOrDash(payload.conflict_type);
  const other = typeof payload.other_worker_id === "string" ? ` with ${payload.other_worker_id}` : "";
  return `${type}${other}`;
}

function sharedCarry(entity: BaseEntityState): string {
  const cargo = entity.attributes.cargo;
  if (!cargo || typeof cargo !== "object") return "-";
  const payload = cargo as Record<string, unknown>;
  const carrierIds = Array.isArray(payload.carrier_ids) ? payload.carrier_ids.map(String).filter(Boolean) : [];
  if (!payload.shared_carry && carrierIds.length <= 1) return "-";
  const multiplier = Number(payload.effective_time_multiplier);
  const multiplierLabel = Number.isFinite(multiplier) ? `, x${multiplier.toFixed(2)}` : "";
  return `${carrierIds.length ? carrierIds.join(" + ") : valueOrDash(payload.carrier_count)}${multiplierLabel}`;
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

export function EntityInspector({ entity, currentTime, selectedRun }: EntityInspectorProps) {
  return (
    <aside className="inspector-panel">
      <div className="panel-title">Inspector</div>
      {selectedRun && <div className="panel-muted">Run: {selectedRun.label}</div>}
      {!entity ? (
        <div className="panel-empty">Select a 3D object to inspect replay attributes.</div>
      ) : (
        <>
          <div className="entity-heading">
            <strong>{entity.label || entity.entity_id}</strong>
            <span>{entity.entity_type}</span>
          </div>
          <dl className="field-grid">
            <dt>ID</dt>
            <dd>{entity.entity_id}</dd>
            <dt>Availability</dt>
            <dd>{valueOrDash(humanoidStateValue(entity, "availability"))}</dd>
            <dt>Mobility</dt>
            <dd>{valueOrDash(humanoidStateValue(entity, "mobility"))}</dd>
            <dt>Power</dt>
            <dd>{valueOrDash(humanoidStateValue(entity, "power"))}</dd>
            <dt>Manipulation</dt>
            <dd>{valueOrDash(humanoidStateValue(entity, "manipulation"))}</dd>
            <dt>Task / Code</dt>
            <dd>{valueOrDash(taskCode(entity))}</dd>
            <dt>Child Task</dt>
            <dd>{valueOrDash(childTaskCode(entity))}</dd>
            <dt>Primitive</dt>
            <dd>{valueOrDash(primitiveCode(entity))}</dd>
            <dt>Cargo</dt>
            <dd>{valueOrDash(cargoItemId(entity))}</dd>
            <dt>Shared Carry</dt>
            <dd>{sharedCarry(entity)}</dd>
            <dt>Motion Path</dt>
            <dd>{motionPathLabel(entity, currentTime)}</dd>
            <dt>Traffic</dt>
            <dd>{trafficConflict(entity, currentTime)}</dd>
          </dl>
          <details className="raw-details">
            <summary>Raw attributes</summary>
            <pre>{prettyJson(entity.attributes)}</pre>
          </details>
        </>
      )}
    </aside>
  );
}
