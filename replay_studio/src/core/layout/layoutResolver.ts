import type { BaseEntityState, XY } from "../types/entity";
import type { LayoutConfig, LayoutNodeConfig, LayoutRegionConfig, LayoutSource } from "../types/layout";
import { buildAutoLayout } from "./autoLayout";

export interface ResolvedLayout {
  viewport: { width: number; height: number };
  positions: Record<string, XY>;
  nodes: Record<string, LayoutNodeConfig>;
  regions: Record<string, LayoutRegionConfig>;
}

function toNodeMap(layout?: LayoutConfig): Record<string, LayoutNodeConfig> {
  return Object.fromEntries((layout?.nodes ?? []).map((node) => [node.entity_id, node]));
}

function toRegionMap(layout?: LayoutConfig): Record<string, LayoutRegionConfig> {
  return Object.fromEntries((layout?.regions ?? []).map((region) => [region.region_id, region]));
}

function positionFromRegion(node: LayoutNodeConfig, regions: Record<string, LayoutRegionConfig>): XY | undefined {
  if (!node.region_id || !node.anchor) return undefined;
  const region = regions[node.region_id];
  if (!region) return undefined;
  return {
    x: region.position.x + region.size.width * node.anchor.x,
    y: region.position.y + region.size.height * node.anchor.y,
  };
}

function mergeRegions(
  autoLayout?: LayoutConfig,
  embeddedLayout?: LayoutConfig,
  externalLayout?: LayoutConfig,
): Record<string, LayoutRegionConfig> {
  return {
    ...toRegionMap(autoLayout),
    ...toRegionMap(embeddedLayout),
    ...toRegionMap(externalLayout),
  };
}

export function resolveLayout(
  entities: BaseEntityState[],
  embeddedLayout?: LayoutConfig,
  externalLayout?: LayoutConfig,
): ResolvedLayout {
  const autoLayout = buildAutoLayout(entities);
  const sourcePriority: LayoutSource[] =
    externalLayout?.source_priority ?? embeddedLayout?.source_priority ?? ["log", "config", "auto"];

  const embeddedNodeMap = toNodeMap(embeddedLayout);
  const externalNodeMap = toNodeMap(externalLayout);
  const autoNodeMap = toNodeMap(autoLayout);
  const regions = mergeRegions(autoLayout, embeddedLayout, externalLayout);
  const viewport = externalLayout?.viewport ?? embeddedLayout?.viewport ?? autoLayout.viewport ?? { width: 1200, height: 760 };

  const positions: Record<string, XY> = {};
  const nodes: Record<string, LayoutNodeConfig> = {};

  for (const entity of entities) {
    for (const source of sourcePriority) {
      if (source === "log" && entity.position) {
        positions[entity.entity_id] = { ...entity.position };
        break;
      }

      if (source === "config") {
        const configNode = externalNodeMap[entity.entity_id] ?? embeddedNodeMap[entity.entity_id];
        if (configNode) {
          positions[entity.entity_id] =
            configNode.position ??
            positionFromRegion(configNode, regions) ??
            positions[entity.entity_id];
          nodes[entity.entity_id] = configNode;
          if (positions[entity.entity_id]) break;
        }
      }

      if (source === "auto") {
        const autoNode = autoNodeMap[entity.entity_id];
        if (autoNode) {
          positions[entity.entity_id] =
            autoNode.position ??
            positionFromRegion(autoNode, regions) ??
            positions[entity.entity_id];
          nodes[entity.entity_id] = autoNode;
          if (positions[entity.entity_id]) break;
        }
      }
    }

    positions[entity.entity_id] ??= { x: viewport.width / 2, y: viewport.height / 2 };
  }

  return { viewport, positions, nodes, regions };
}
