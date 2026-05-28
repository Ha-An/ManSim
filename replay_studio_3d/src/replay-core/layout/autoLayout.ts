import type { BaseEntityState, EntityType } from "../types/entity";
import type { LayoutConfig, LayoutNodeConfig } from "../types/layout";

const DEFAULT_VIEWPORT = { width: 1200, height: 760 };

const typeAnchors: Record<EntityType, { x: number; y: number }> = {
  queue: { x: 180, y: 220 },
  storage: { x: 180, y: 520 },
  shelf: { x: 180, y: 520 },
  material_slot: { x: 180, y: 560 },
  buffer: { x: 360, y: 220 },
  scrap_queue: { x: 360, y: 420 },
  scrap_bin: { x: 980, y: 420 },
  inspection_table: { x: 760, y: 420 },
  item: { x: 720, y: 620 },
  machine: { x: 560, y: 220 },
  workstation: { x: 560, y: 420 },
  charger: { x: 980, y: 540 },
  maintenance_station: { x: 980, y: 360 },
  worker: { x: 320, y: 660 },
  robot: { x: 320, y: 660 },
  transporter: { x: 320, y: 660 },
  order: { x: 860, y: 140 },
  task: { x: 860, y: 240 },
};

export function buildAutoLayout(entities: BaseEntityState[]): LayoutConfig {
  const counters: Partial<Record<EntityType, number>> = {};
  const nodes: LayoutNodeConfig[] = entities
    .slice()
    .sort((left, right) => left.entity_id.localeCompare(right.entity_id))
    .map((entity) => {
      counters[entity.entity_type] = (counters[entity.entity_type] ?? 0) + 1;
      const laneIndex = (counters[entity.entity_type] ?? 1) - 1;
      const anchor = typeAnchors[entity.entity_type] ?? { x: 600, y: 380 };
      const x = anchor.x + (laneIndex % 3) * 120;
      const y = anchor.y + Math.floor(laneIndex / 3) * 90;
      return {
        entity_id: entity.entity_id,
        entity_type: entity.entity_type,
        position: { x, y },
      };
    });

  return {
    source_priority: ["config", "auto"],
    viewport: DEFAULT_VIEWPORT,
    nodes,
  };
}
