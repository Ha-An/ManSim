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
        const color = packetColor(flow.severity);
        const packetCount = isCurrent ? 4 : isMovement ? 3 : 2;
        const speed = flow.severity === "warning" ? 0.14 : flow.severity === "error" ? 0.18 : 0.11;

        return (
          <g key={flow.id}>
            <line
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              stroke={color}
              strokeWidth={isCurrent ? 1.8 : isMovement ? 1.55 : 0.8}
              strokeOpacity={isCurrent ? 0.24 : isMovement ? 0.22 : 0.06}
            />

            {Array.from({ length: packetCount }).map((_, index) => {
              const trailOffset = index / packetCount;
              const progress = (currentTime * speed + trailOffset) % 1;
              const point = packetPoint(source, target, progress);
              const radius = isCurrent && index === packetCount - 1 ? 3.2 : isMovement ? 2.2 : 1.8;
              const opacity = 0.12 + ((index + 1) / packetCount) * (isCurrent ? 0.56 : isMovement ? 0.42 : 0.24);
              return <circle key={`${flow.id}:${index}`} cx={point.x} cy={point.y} r={radius} fill={color} fillOpacity={opacity} />;
            })}

            <circle cx={source.x} cy={source.y} r={isCurrent ? 2.4 : isMovement ? 1.9 : 1.6} fill={color} fillOpacity={isCurrent ? 0.65 : isMovement ? 0.44 : 0.24} />
            <circle cx={target.x} cy={target.y} r={isCurrent ? 2.4 : isMovement ? 1.9 : 1.6} fill={color} fillOpacity={isCurrent ? 0.65 : isMovement ? 0.44 : 0.24} />
          </g>
        );
      })}
    </svg>
  );
}
