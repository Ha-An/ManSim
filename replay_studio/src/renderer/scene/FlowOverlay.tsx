import type { ReplayEvent } from "../../core/types/event";
import type { ReplayRenderModel } from "../../core/types/replay";

interface FlowOverlayProps {
  width: number;
  height: number;
  viewport: { width: number; height: number };
  renderModel: ReplayRenderModel;
  currentEvent?: ReplayEvent;
  currentTime: number;
}

function project(position: { x: number; y: number }, viewport: { width: number; height: number }, width: number, height: number) {
  const scale = Math.min(width / viewport.width, height / viewport.height);
  const offsetX = (width - viewport.width * scale) / 2;
  const offsetY = (height - viewport.height * scale) / 2;
  return { x: offsetX + position.x * scale, y: offsetY + position.y * scale };
}

function packetPoint(source: { x: number; y: number }, target: { x: number; y: number }, progress: number) {
  return {
    x: source.x + (target.x - source.x) * progress,
    y: source.y + (target.y - source.y) * progress,
  };
}

function packetColor(severity?: "info" | "warning" | "error") {
  if (severity === "error") return "#ff7189";
  if (severity === "warning") return "#ffcd63";
  return "#4d8dff";
}

function movementColor() {
  return "#0b6cff";
}

function arrowPolygon(point: { x: number; y: number }, angle: number, size: number) {
  const tip = {
    x: point.x + Math.cos(angle) * size,
    y: point.y + Math.sin(angle) * size,
  };
  const left = {
    x: point.x + Math.cos(angle + Math.PI * 0.78) * size * 0.72,
    y: point.y + Math.sin(angle + Math.PI * 0.78) * size * 0.72,
  };
  const right = {
    x: point.x + Math.cos(angle - Math.PI * 0.78) * size * 0.72,
    y: point.y + Math.sin(angle - Math.PI * 0.78) * size * 0.72,
  };
  return `${tip.x},${tip.y} ${left.x},${left.y} ${right.x},${right.y}`;
}

function asXY(value: unknown): { x: number; y: number } | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Record<string, unknown>;
  const x = Number(candidate.x);
  const y = Number(candidate.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return undefined;
  return { x, y };
}

function motionRoute(node: ReplayRenderModel["nodes"][number]): Array<{ x: number; y: number }> {
  const motion = node.entity.attributes.motion;
  if (!motion || typeof motion !== "object") return [];
  const rawPath = (motion as Record<string, unknown>).display_path ?? (motion as Record<string, unknown>).path;
  if (Array.isArray(rawPath)) {
    const path = rawPath.map(asXY).filter((point): point is { x: number; y: number } => Boolean(point));
    if (path.length >= 2) return path;
  }
  const from = asXY((motion as Record<string, unknown>).from);
  const to = asXY((motion as Record<string, unknown>).to);
  return from && to ? [from, to] : [];
}

function pathDistance(points: Array<{ x: number; y: number }>): number {
  let total = 0;
  for (let index = 1; index < points.length; index += 1) {
    total += Math.hypot(points[index].x - points[index - 1].x, points[index].y - points[index - 1].y);
  }
  return total;
}

function samplePath(
  points: Array<{ x: number; y: number }>,
  progress: number,
): { point: { x: number; y: number }; angle: number } | undefined {
  if (points.length < 2) return undefined;
  const total = pathDistance(points);
  if (total <= 0) return undefined;
  const targetDistance = Math.max(0, Math.min(1, progress)) * total;
  let walked = 0;
  for (let index = 1; index < points.length; index += 1) {
    const source = points[index - 1];
    const target = points[index];
    const segment = Math.hypot(target.x - source.x, target.y - source.y);
    if (segment <= 0) continue;
    if (walked + segment >= targetDistance) {
      const local = (targetDistance - walked) / segment;
      return {
        point: packetPoint(source, target, local),
        angle: Math.atan2(target.y - source.y, target.x - source.x),
      };
    }
    walked += segment;
  }
  const previous = points[points.length - 2];
  const last = points[points.length - 1];
  return {
    point: last,
    angle: Math.atan2(last.y - previous.y, last.x - previous.x),
  };
}

function hasActiveMotion(node: ReplayRenderModel["nodes"][number], currentTime: number): boolean {
  const motion = node.entity.attributes.motion;
  if (!motion || typeof motion !== "object") return false;
  const startedAt = Number((motion as Record<string, unknown>).started_at);
  const endedAt = Number((motion as Record<string, unknown>).ended_at);
  return Number.isFinite(startedAt) && Number.isFinite(endedAt) && currentTime >= startedAt && currentTime <= endedAt;
}

function resolveAnchor(
  id: string | undefined,
  renderModel: ReplayRenderModel,
): { x: number; y: number } | undefined {
  if (!id) return undefined;
  const node = renderModel.nodes.find((candidate) => candidate.entity.entity_id === id);
  if (node) return node.position;
  const region = renderModel.regions.find((candidate) => candidate.region_id === id);
  if (region) {
    return {
      x: region.position.x + region.size.width / 2,
      y: region.position.y + region.size.height / 2,
    };
  }
  return undefined;
}

export function FlowOverlay({ width, height, viewport, renderModel, currentEvent, currentTime }: FlowOverlayProps) {
  const conflictPayload = currentEvent?.event_type === "traffic_conflict_detected" ? currentEvent.payload : undefined;
  const conflictTile = conflictPayload ? asXY(conflictPayload.tile_position) : undefined;
  const conflictEdgeFrom = conflictPayload ? asXY(conflictPayload.edge_from_position) : undefined;
  const conflictEdgeTo = conflictPayload ? asXY(conflictPayload.edge_to_position) : undefined;
  const conflictColor = conflictPayload?.severity === "error" ? "#ff315a" : conflictPayload?.severity === "info" ? "#4d8dff" : "#ffb703";
  return (
    <svg className="scene-overlay" width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {renderModel.flows.map((flow) => {
        if (!flow.source_id || !flow.target_id) return null;
        if (flow.kind === "movement") return null;
        const sourcePosition = resolveAnchor(flow.source_id, renderModel);
        const targetPosition = resolveAnchor(flow.target_id, renderModel);
        if (!sourcePosition || !targetPosition) return null;

        const source = project(sourcePosition, viewport, width, height);
        const target = project(targetPosition, viewport, width, height);
        const isCurrent = flow.id === `current:${currentEvent?.event_id}`;
        const color = packetColor(flow.severity);
        const packetCount = isCurrent ? 4 : 2;
        const speed = flow.severity === "warning" ? 0.14 : flow.severity === "error" ? 0.18 : 0.11;

        return (
          <g key={flow.id}>
            <line
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              stroke={color}
              strokeWidth={isCurrent ? 1.8 : 0.8}
              strokeOpacity={isCurrent ? 0.3 : 0.06}
            />

            {Array.from({ length: packetCount }).map((_, index) => {
              const trailOffset = index / packetCount;
              const progress = (currentTime * speed + trailOffset) % 1;
              const point = packetPoint(source, target, progress);
              const opacity = 0.12 + ((index + 1) / packetCount) * (isCurrent ? 0.56 : 0.24);
              const radius = isCurrent && index === packetCount - 1 ? 3.2 : 1.8;
              return <circle key={`${flow.id}:${index}`} cx={point.x} cy={point.y} r={radius} fill={color} fillOpacity={opacity} />;
            })}

            <circle cx={source.x} cy={source.y} r={isCurrent ? 2.4 : 1.6} fill={color} fillOpacity={isCurrent ? 0.65 : 0.24} />
            <circle cx={target.x} cy={target.y} r={isCurrent ? 2.4 : 1.6} fill={color} fillOpacity={isCurrent ? 0.65 : 0.24} />
          </g>
        );
      })}
      {conflictTile && (
        <g key="traffic-conflict-tile">
          {(() => {
            const point = project(conflictTile, viewport, width, height);
            return (
              <>
                <circle cx={point.x} cy={point.y} r={13} fill={conflictColor} fillOpacity={0.18} />
                <circle cx={point.x} cy={point.y} r={7} fill="none" stroke={conflictColor} strokeWidth={2.4} strokeOpacity={0.82} />
              </>
            );
          })()}
        </g>
      )}
      {conflictEdgeFrom && conflictEdgeTo && (
        <g key="traffic-conflict-edge">
          {(() => {
            const source = project(conflictEdgeFrom, viewport, width, height);
            const target = project(conflictEdgeTo, viewport, width, height);
            return (
              <line
                x1={source.x}
                y1={source.y}
                x2={target.x}
                y2={target.y}
                stroke={conflictColor}
                strokeWidth={4}
                strokeOpacity={0.72}
                strokeLinecap="round"
                strokeDasharray="6 4"
              />
            );
          })()}
        </g>
      )}
      {renderModel.nodes.map((node) => {
        if (!hasActiveMotion(node, currentTime)) return null;
        const route = motionRoute(node).map((point) => project(point, viewport, width, height));
        const distance = pathDistance(route);
        if (distance < 4) return null;
        const color = movementColor();
        const routePoints = route.map((point) => `${point.x},${point.y}`).join(" ");
        return (
          <g key={`motion:${node.entity.entity_id}`}>
            <polyline
              points={routePoints}
              stroke={color}
              strokeWidth={1.0}
              strokeOpacity={0.58}
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeDasharray="9 5"
              fill="none"
            />
            {[0.36, 0.66, 0.9].map((progress, index) => {
              const sample = samplePath(route, progress);
              if (!sample) return null;
              return (
                <polygon
                  key={`motion:${node.entity.entity_id}:${index}`}
                  points={arrowPolygon(sample.point, sample.angle, 4.4)}
                  fill={color}
                  fillOpacity={0.58 + index * 0.12}
                />
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}
