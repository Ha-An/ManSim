import { useEffect, useMemo, useRef, useState } from "react";
import type { ReplayEvent } from "../../core/types/event";
import type { ReplayRenderModel } from "../../core/types/replay";
import { FlowOverlay } from "./FlowOverlay";
import { SceneCanvas } from "./SceneCanvas";
import { SelectionOverlay } from "./SelectionOverlay";

interface SceneLayerProps {
  renderModel: ReplayRenderModel;
  currentEvent?: ReplayEvent;
  currentTime: number;
  viewport: { width: number; height: number };
  onSelectEntity?: (entityId: string) => void;
}

export function SceneLayer({ renderModel, currentEvent, currentTime, viewport, onSelectEntity }: SceneLayerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(960);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const height = useMemo(() => Math.max(480, Math.min(760, width * (viewport.height / viewport.width))), [viewport.height, viewport.width, width]);

  return (
    <div ref={containerRef} className="scene-shell">
      {/* Canvas handles dense node animation; SVG overlays handle arrows and selection cleanly. */}
      <div className="scene-stack" style={{ height }}>
        <SceneCanvas width={width} height={height} viewport={viewport} renderModel={renderModel} currentEvent={currentEvent} currentTime={currentTime} onSelectEntity={onSelectEntity} />
        <FlowOverlay width={width} height={height} viewport={viewport} renderModel={renderModel} currentEvent={currentEvent} currentTime={currentTime} />
        <SelectionOverlay width={width} height={height} viewport={viewport} renderModel={renderModel} />
      </div>
    </div>
  );
}
