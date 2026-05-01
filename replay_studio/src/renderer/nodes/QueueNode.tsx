import type { NodeStyle } from "./MachineNode";

export function getQueueNodeStyle(): NodeStyle {
  return {
    fill: "#121d30",
    stroke: "#35557b",
    accent: "#7ad1ff",
    text: "#f5f8ff",
    glow: "rgba(122, 209, 255, 0.16)",
  };
}
