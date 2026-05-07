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

function motionTarget(node: ReplayRenderModel["nodes"][number]): { x: number; y: number } | undefined {
  const motion = node.entity.attributes.motion;
  if (!motion || typeof motion !== "object") return undefined;
  return asXY((motion as Record<string, unknown>).to);
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
  return (
    <svg className="scene-overlay" width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {renderModel.flows.map((flow) => {
        if (!flow.source_id || !flow.target_id) return null;
        const sourcePosition = resolveAnchor(flow.source_id, renderModel);
        const targetPosition = resolveAnchor(flow.target_id, renderModel);
        if (!sourcePosition || !targetPosition) return null;

        const source = project(sourcePosition, viewport, width, height);
        const target = project(targetPosition, viewport, width, height);
        const isCurrent = flow.id === `current:${currentEvent?.event_id}`;
        const isMovement = flow.kind === "movement";
        const color = isMovement ? movementColor() : packetColor(flow.severity);
        const packetCount = isCurrent ? 4 : isMovement ? 3 : 2;
        const speed = flow.severity === "warning" ? 0.14 : flow.severity === "error" ? 0.18 : 0.11;
        const angle = Math.atan2(target.y - source.y, target.x - source.x);

        return (
          <g key={flow.id}>
            <line
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              stroke={color}
              strokeWidth={isCurrent ? 1.8 : isMovement ? 1.0 : 0.8}
              strokeOpacity={isCurrent ? 0.3 : isMovement ? 0.52 : 0.06}
              strokeDasharray={isMovement ? "8 5" : undefined}
            />

            {Array.from({ length: packetCount }).map((_, index) => {
              const trailOffset = index / packetCount;
              const progress = (currentTime * speed + trailOffset) % 1;
              const point = packetPoint(source, target, progress);
              const opacity = 0.12 + ((index + 1) / packetCount) * (isCurrent ? 0.56 : isMovement ? 0.42 : 0.24);
              if (isMovement) {
                return (
                  <polygon
                    key={`${flow.id}:${index}`}
                    points={arrowPolygon(point, angle, isCurrent && index === packetCount - 1 ? 5.2 : 4.2)}
                    fill={color}
                    fillOpacity={opacity + 0.18}
                  />
                );
              }
              const radius = isCurrent && index === packetCount - 1 ? 3.2 : 1.8;
              return <circle key={`${flow.id}:${index}`} cx={point.x} cy={point.y} r={radius} fill={color} fillOpacity={opacity} />;
            })}

            {!isMovement && (
              <>
                <circle cx={source.x} cy={source.y} r={isCurrent ? 2.4 : 1.6} fill={color} fillOpacity={isCurrent ? 0.65 : 0.24} />
                <circle cx={target.x} cy={target.y} r={isCurrent ? 2.4 : 1.6} fill={color} fillOpacity={isCurrent ? 0.65 : 0.24} />
              </>
            )}
          </g>
        );
      })}
      {renderModel.nodes.map((node) => {
        if (node.entity.state !== "moving") return null;
        const targetPosition = motionTarget(node);
        if (!targetPosition) return null;
        const source = project(node.position, viewport, width, height);
        const target = project(targetPosition, viewport, width, height);
        const dx = target.x - source.x;
        const dy = target.y - source.y;
        const distance = Math.hypot(dx, dy);
        if (distance < 4) return null;
        const angle = Math.atan2(dy, dx);
        const color = movementColor();
        return (
          <g key={`motion:${node.entity.entity_id}`}>
            <line
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              stroke={color}
              strokeWidth={1.0}
              strokeOpacity={0.58}
              strokeLinecap="round"
              strokeDasharray="9 5"
            />
            {[0.36, 0.66, 0.9].map((progress, index) => {
              const point = packetPoint(source, target, progress);
              return (
                <polygon
                  key={`motion:${node.entity.entity_id}:${index}`}
                  points={arrowPolygon(point, angle, 4.4)}
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
