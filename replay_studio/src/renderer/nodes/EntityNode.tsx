import type { BaseEntityState } from "../../core/types/entity";
import { getChargerNodeStyle } from "./ChargerNode";
import { getMachineNodeStyle, type NodeStyle } from "./MachineNode";
import { getQueueNodeStyle } from "./QueueNode";
import { getWorkerNodeStyle } from "./RobotNode";

export interface NodeSize {
  width: number;
  height: number;
}

export function getNodeSize(entity: BaseEntityState): NodeSize {
  switch (entity.entity_type) {
    case "worker":
    case "robot":
    case "transporter":
      return { width: 108, height: 84 };
    case "machine":
    case "workstation":
      return { width: 120, height: 110 };
    case "queue":
    case "buffer":
    case "storage":
      return { width: 92, height: 72 };
    case "charger":
    case "maintenance_station":
      return { width: 92, height: 76 };
    default:
      return { width: 96, height: 72 };
  }
}

export function getEntityNodeStyle(entity: BaseEntityState): NodeStyle {
  if (entity.entity_type === "machine" || entity.entity_type === "workstation") {
    return getMachineNodeStyle(entity.state);
  }
  if (entity.entity_type === "worker" || entity.entity_type === "robot" || entity.entity_type === "transporter") {
    return getWorkerNodeStyle(entity.state);
  }
  if (entity.entity_type === "queue" || entity.entity_type === "buffer" || entity.entity_type === "storage") {
    return getQueueNodeStyle();
  }
  if (entity.entity_type === "charger" || entity.entity_type === "maintenance_station") {
    return getChargerNodeStyle();
  }
  return getQueueNodeStyle();
}
