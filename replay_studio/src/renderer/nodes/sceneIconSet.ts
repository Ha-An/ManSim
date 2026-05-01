import { loadPixelArtFrame, type PixelArtFrame } from "./pixelArtFrame";

export interface SceneIconFrame extends PixelArtFrame {}

export interface SceneIconSet {
  queue: SceneIconFrame;
  material: SceneIconFrame;
  intermediate: SceneIconFrame;
  product: SceneIconFrame;
  battery: SceneIconFrame;
  inspectFacility: SceneIconFrame;
}

const SCENE_ICON_URLS = {
  queue: "/assets/queue.png",
  material: "/assets/material.png",
  intermediate: "/assets/intermediate.png",
  product: "/assets/product.png",
  battery: "/assets/battery.png",
  inspectFacility: "/assets/facility/inspec.png",
} as const;

let sceneIconPromise: Promise<SceneIconSet> | null = null;

export async function loadSceneIconSet(): Promise<SceneIconSet> {
  if (sceneIconPromise) return sceneIconPromise;

  sceneIconPromise = (async () => {
    const [queue, material, intermediate, product, battery, inspectFacility] = await Promise.all([
      loadPixelArtFrame(SCENE_ICON_URLS.queue, { removeCheckerboard: true, minOpaqueRatio: 0.02 }),
      loadPixelArtFrame(SCENE_ICON_URLS.material, { removeLightBackground: true, minOpaqueRatio: 0.02 }),
      loadPixelArtFrame(SCENE_ICON_URLS.intermediate, { removeCheckerboard: true, minOpaqueRatio: 0.02 }),
      loadPixelArtFrame(SCENE_ICON_URLS.product, { removeCheckerboard: true, minOpaqueRatio: 0.02 }),
      loadPixelArtFrame(SCENE_ICON_URLS.battery, { removeCheckerboard: true, minOpaqueRatio: 0.02 }),
      loadPixelArtFrame(SCENE_ICON_URLS.inspectFacility, { removeLightBackground: true, minOpaqueRatio: 0.02 }),
    ]);
    return { queue, material, intermediate, product, battery, inspectFacility };
  })();

  return sceneIconPromise;
}
