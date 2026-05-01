import type { BaseEntityState } from "../../core/types/entity";

export interface MachineSpriteFrame {
  canvas: HTMLCanvasElement;
  width: number;
  height: number;
}

export interface MachineSpriteSet {
  waiting: MachineSpriteFrame;
  processing: MachineSpriteFrame;
  down: MachineSpriteFrame;
}

const MACHINE_FRAME_URLS = {
  waiting: "/assets/facility_processed/Waiting.png",
  processing: "/assets/facility_processed/Processing.png",
  down: "/assets/facility_processed/Down.png",
} as const;

let machineSpritePromise: Promise<MachineSpriteSet> | null = null;

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`Failed to load machine image: ${url}`));
    image.src = url;
  });
}

async function loadFrame(url: string): Promise<MachineSpriteFrame> {
  const image = await loadImage(url);
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error(`Unable to create machine canvas for ${url}`);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(image, 0, 0);
  return { canvas, width: canvas.width, height: canvas.height };
}

export async function loadMachineSpriteSet(): Promise<MachineSpriteSet> {
  if (machineSpritePromise) return machineSpritePromise;
  machineSpritePromise = (async () => {
    const [waiting, processing, down] = await Promise.all([
      loadFrame(MACHINE_FRAME_URLS.waiting),
      loadFrame(MACHINE_FRAME_URLS.processing),
      loadFrame(MACHINE_FRAME_URLS.down),
    ]);
    return { waiting, processing, down };
  })();
  return machineSpritePromise;
}

export function getMachineSpriteFrame(
  machineSprites: MachineSpriteSet | null,
  entity: Pick<BaseEntityState, "state" | "attributes">,
): MachineSpriteFrame | null {
  if (!machineSprites) return null;

  const attributes = entity.attributes ?? {};
  const machineState = typeof attributes.machine_state === "string" ? attributes.machine_state.toUpperCase() : "";

  if (
    entity.state === "error" ||
    entity.state === "maintenance" ||
    machineState.includes("BROKEN") ||
    machineState.includes("REPAIR") ||
    machineState.includes("PM")
  ) {
    return machineSprites.down;
  }

  if (machineState.includes("PROCESS")) {
    return machineSprites.processing;
  }

  if (!machineState && entity.state === "working") {
    return machineSprites.processing;
  }

  return machineSprites.waiting;
}
