import type { ReplayRenderModel } from "../../core/types/replay";

interface SelectionOverlayProps {
  width: number;
  height: number;
  viewport: { width: number; height: number };
  renderModel: ReplayRenderModel;
}

function project(position: { x: number; y: number }, viewport: { width: number; height: number }, width: number, height: number) {
  const scale = Math.min(width / viewport.width, height / viewport.height);
  const offsetX = (width - viewport.width * scale) / 2;
  const offsetY = (height - viewport.height * scale) / 2;
  return { x: offsetX + position.x * scale, y: offsetY + position.y * scale, scale };
}

export function SelectionOverlay({ width, height, viewport, renderModel }: SelectionOverlayProps) {
  const selectedNode = renderModel.nodes.find((node) => node.selected);
  if (!selectedNode) return null;
  const point = project(selectedNode.position, viewport, width, height);
  const size = 68 * point.scale * 0.5;

  return (
    <svg className="scene-overlay" width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      <rect
        x={point.x - size}
        y={point.y - size}
        width={size * 2}
        height={size * 2}
        fill="none"
        stroke="#68c3ff"
        strokeWidth="2"
        strokeDasharray="8 6"
        opacity="0.9"
      />
    </svg>
  );
}
