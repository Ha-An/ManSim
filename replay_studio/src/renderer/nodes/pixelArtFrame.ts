export interface PixelArtFrame {
  canvas: HTMLCanvasElement;
  width: number;
  height: number;
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`Failed to load image: ${url}`));
    image.src = url;
  });
}

function isLikelyCheckerColor(r: number, g: number, b: number): boolean {
  const closeToA = Math.abs(r - 244) <= 10 && Math.abs(g - 244) <= 10 && Math.abs(b - 244) <= 10;
  const closeToB = Math.abs(r - 254) <= 6 && Math.abs(g - 254) <= 6 && Math.abs(b - 254) <= 6;
  const lowChroma = Math.abs(r - g) <= 6 && Math.abs(g - b) <= 6;
  return lowChroma && (closeToA || closeToB);
}

function isLightBackgroundColor(r: number, g: number, b: number): boolean {
  const nearWhite = r >= 238 && g >= 238 && b >= 238;
  const lowChroma = Math.abs(r - g) <= 10 && Math.abs(g - b) <= 10;
  return nearWhite && lowChroma;
}

function floodRemoveBackground(
  canvas: HTMLCanvasElement,
  matcher: (r: number, g: number, b: number) => boolean,
): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const { width, height } = canvas;
  const imageData = ctx.getImageData(0, 0, width, height);
  const originalCopy = new Uint8ClampedArray(imageData.data);
  const data = imageData.data;
  const visited = new Uint8Array(width * height);
  const queue: number[] = [];

  const pushIfCandidate = (x: number, y: number) => {
    if (x < 0 || y < 0 || x >= width || y >= height) return;
    const idx = y * width + x;
    if (visited[idx]) return;
    const offset = idx * 4;
    const alpha = data[offset + 3];
    if (alpha === 0) {
      visited[idx] = 1;
      return;
    }
    if (!matcher(data[offset], data[offset + 1], data[offset + 2])) return;
    visited[idx] = 1;
    queue.push(idx);
  };

  for (let x = 0; x < width; x += 1) {
    pushIfCandidate(x, 0);
    pushIfCandidate(x, height - 1);
  }
  for (let y = 0; y < height; y += 1) {
    pushIfCandidate(0, y);
    pushIfCandidate(width - 1, y);
  }

  while (queue.length > 0) {
    const idx = queue.shift()!;
    const x = idx % width;
    const y = Math.floor(idx / width);
    const offset = idx * 4;
    data[offset + 3] = 0;

    pushIfCandidate(x - 1, y);
    pushIfCandidate(x + 1, y);
    pushIfCandidate(x, y - 1);
    pushIfCandidate(x, y + 1);
  }

  let remainingOpaque = 0;
  let originalOpaque = 0;
  for (let i = 3; i < data.length; i += 4) {
    if (data[i] > 0) remainingOpaque += 1;
  }

  for (let i = 3; i < originalCopy.length; i += 4) {
    if (originalCopy[i] > 0) originalOpaque += 1;
  }

  if (originalOpaque > 0 && remainingOpaque < originalOpaque * 0.2) {
    return;
  }

  ctx.putImageData(imageData, 0, 0);
}

function removeCheckerboardBackground(canvas: HTMLCanvasElement): void {
  floodRemoveBackground(canvas, isLikelyCheckerColor);
}

function removeLightBackground(canvas: HTMLCanvasElement): void {
  floodRemoveBackground(canvas, (r, g, b) => isLikelyCheckerColor(r, g, b) || isLightBackgroundColor(r, g, b));
}

export async function loadPixelArtFrame(
  url: string,
  options?: {
    removeCheckerboard?: boolean;
    removeLightBackground?: boolean;
    minOpaqueRatio?: number;
  },
): Promise<PixelArtFrame> {
  const image = await loadImage(url);
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error(`Unable to create frame canvas for ${url}`);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(image, 0, 0);

  if (options?.removeCheckerboard || options?.removeLightBackground) {
    // Keep a copy so aggressive removal can be rolled back safely.
    const original = document.createElement("canvas");
    original.width = canvas.width;
    original.height = canvas.height;
    const originalCtx = original.getContext("2d");
    if (originalCtx) {
      originalCtx.imageSmoothingEnabled = false;
      originalCtx.drawImage(canvas, 0, 0);
    }

    const before = ctx.getImageData(0, 0, canvas.width, canvas.height);
    let beforeOpaque = 0;
    for (let i = 3; i < before.data.length; i += 4) {
      if (before.data[i] > 0) beforeOpaque += 1;
    }

    if (options?.removeLightBackground) {
      removeLightBackground(canvas);
    } else {
      removeCheckerboardBackground(canvas);
    }

    const after = ctx.getImageData(0, 0, canvas.width, canvas.height);
    let afterOpaque = 0;
    for (let i = 3; i < after.data.length; i += 4) {
      if (after.data[i] > 0) afterOpaque += 1;
    }

    const minOpaqueRatio = options?.minOpaqueRatio ?? 0.35;
    if (beforeOpaque > 0 && afterOpaque < beforeOpaque * minOpaqueRatio && originalCtx) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(original, 0, 0);
    }
  }

  return { canvas, width: canvas.width, height: canvas.height };
}
