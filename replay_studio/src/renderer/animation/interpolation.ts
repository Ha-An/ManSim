import { easeInOutCubic } from "./easing";

export function interpolate(start: number, end: number, ratio: number): number {
  const eased = easeInOutCubic(Math.min(1, Math.max(0, ratio)));
  return start + (end - start) * eased;
}
