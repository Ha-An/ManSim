import type { EntityStatus } from "../../core/types/entity";
import type { NodeStyle } from "./MachineNode";

export function getRobotNodeStyle(status: EntityStatus): NodeStyle {
  const base = {
    fill: "#0f1626",
    stroke: "#324767",
    accent: "#5fa8ff",
    text: "#f5f8ff",
    glow: "rgba(95, 168, 255, 0.18)",
  };

  if (status === "moving") return { ...base, accent: "#66e0ff", glow: "rgba(102, 224, 255, 0.2)" };
  if (status === "working") return { ...base, accent: "#6ed38b", glow: "rgba(110, 211, 139, 0.18)" };
  if (status === "charging") return { ...base, accent: "#f3c95a", glow: "rgba(243, 201, 90, 0.18)" };
  if (status === "error") return { ...base, accent: "#ff6f7d", glow: "rgba(255, 111, 125, 0.2)" };
  return base;
}

export const getWorkerNodeStyle = getRobotNodeStyle;
