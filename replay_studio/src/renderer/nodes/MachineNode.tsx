import type { EntityStatus } from "../../core/types/entity";

export interface NodeStyle {
  fill: string;
  stroke: string;
  accent: string;
  text: string;
  glow: string;
}

export function getMachineNodeStyle(status: EntityStatus): NodeStyle {
  const base = {
    fill: "#111b2c",
    stroke: "#2d4268",
    accent: "#8fa7d2",
    text: "#f5f8ff",
    glow: "rgba(71, 121, 255, 0.15)",
  };

  if (status === "working") return { ...base, accent: "#4ad295", glow: "rgba(74, 210, 149, 0.2)" };
  if (status === "blocked") return { ...base, accent: "#f2b94b", glow: "rgba(242, 185, 75, 0.18)" };
  if (status === "maintenance") return { ...base, accent: "#7c6df2", glow: "rgba(124, 109, 242, 0.2)" };
  if (status === "error") return { ...base, accent: "#ff5d73", glow: "rgba(255, 93, 115, 0.22)" };
  return base;
}
