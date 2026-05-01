import type { BaseEntityState } from "../../core/types/entity";
import { loadPixelArtFrame, type PixelArtFrame } from "./pixelArtFrame";
import { getWorkerVisualState, type WorkerSpriteVariant } from "./workerVisualState";

export interface WorkerSpriteFrame extends PixelArtFrame {}

export interface WorkerSpriteSheet {
  animations: {
    idle: WorkerSpriteFrame[];
    walk: WorkerSpriteFrame[];
    carry: WorkerSpriteFrame[];
    setup: WorkerSpriteFrame[];
    unload: WorkerSpriteFrame[];
    fix: WorkerSpriteFrame[];
    discharged: WorkerSpriteFrame[];
  };
}

const WORKER_FRAME_URLS = {
  idle: ["/assets/worker_processed/Idle1.png"],
  walk: ["/assets/worker_processed/Walk1.png", "/assets/worker_processed/Walk2.png"],
  carry: ["/assets/worker_processed/Delivery1.png", "/assets/worker_processed/Delivery2.png"],
  setup: ["/assets/worker_processed/setup1.png", "/assets/worker_processed/setup2.png"],
  unload: ["/assets/worker_processed/unload1.png", "/assets/worker_processed/unload2.png"],
  fix: ["/assets/worker_processed/Fix1.png", "/assets/worker_processed/Fix2.png"],
  discharged: ["/assets/worker_processed/Discharged1.png", "/assets/worker_processed/Discharged2.png"],
} as const;

let spriteSheetPromise: Promise<WorkerSpriteSheet> | null = null;

export async function loadWorkerSpriteSheet(): Promise<WorkerSpriteSheet> {
  if (spriteSheetPromise) return spriteSheetPromise;

  spriteSheetPromise = (async () => {
    const getFrames = async (animation: keyof typeof WORKER_FRAME_URLS): Promise<WorkerSpriteFrame[]> =>
      Promise.all(WORKER_FRAME_URLS[animation].map((url) => loadPixelArtFrame(url)));
    const [idle, walk, carry, setup, unload, fix, discharged] = await Promise.all([
      getFrames("idle"),
      getFrames("walk"),
      getFrames("carry"),
      getFrames("setup"),
      getFrames("unload"),
      getFrames("fix"),
      getFrames("discharged"),
    ]);

    return {
      animations: {
        idle,
        walk,
        carry,
        setup,
        unload,
        fix,
        discharged,
      },
    };
  })();

  return spriteSheetPromise;
}

export function getWorkerSpriteVariant(entity: Pick<BaseEntityState, "state" | "attributes">): WorkerSpriteVariant {
  return getWorkerVisualState(entity).spriteVariant;
}

export function getWorkerSpriteThumbUrl(entity: Pick<BaseEntityState, "state" | "attributes">): string {
  const variant = getWorkerSpriteVariant(entity);
  const frames = WORKER_FRAME_URLS[variant];
  return frames[0];
}

export function getWorkerSpriteFrame(
  spriteSheet: WorkerSpriteSheet | null,
  entity: Pick<BaseEntityState, "state" | "attributes">,
  currentTime: number,
): WorkerSpriteFrame | null {
  if (!spriteSheet) return null;

  const variant = getWorkerSpriteVariant(entity);
  const frames = spriteSheet.animations[variant];
  const speed = variant === "walk" || variant === "carry" || variant === "setup" || variant === "unload" || variant === "fix" ? 4 : 2;
  return frames[Math.floor(currentTime * speed) % frames.length] ?? frames[0] ?? null;
}
