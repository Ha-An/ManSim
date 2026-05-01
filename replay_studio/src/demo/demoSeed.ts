import type { ReplayLog } from "../core/types/replay";

export const demoSeed: Pick<ReplayLog["metadata"], "title" | "description"> = {
  title: "Manufacturing Worker Replay",
  description: "Three workers, four machines, queues, charging, inspection, and incident flow reconstructed from a real ManSim run.",
};
