export const DEFAULT_DEMO_LOG_PATH = "/demo/mansim_existing_run_log.json";
export const LOG_QUERY_PARAM = "log";
export const MANIFEST_QUERY_PARAM = "manifest";
export const RUN_QUERY_PARAM = "run";

export type ManifestRun = {
  id: string;
  label: string;
  output_dir?: string;
  artifacts?: Record<string, string>;
};

export type DashboardManifest = {
  current_run?: string;
  runs?: ManifestRun[];
};

export function toFetchablePath(rawPath: string | null | undefined): string | null {
  const text = String(rawPath ?? "").trim();
  if (!text) return null;
  if (text.startsWith("http://") || text.startsWith("https://") || text.startsWith("/@fs/")) {
    return text;
  }
  if (/^[A-Za-z]:[\\/]/.test(text)) {
    return `/__mansim_file?path=${encodeURIComponent(text)}`;
  }
  return text;
}

export function getRequestedLogPath(search: string = window.location.search): string {
  const params = new URLSearchParams(search);
  return params.get(LOG_QUERY_PARAM) || DEFAULT_DEMO_LOG_PATH;
}

export function getRequestedManifestPath(search: string = window.location.search): string | null {
  const params = new URLSearchParams(search);
  return params.get(MANIFEST_QUERY_PARAM);
}

export function getRequestedRunId(search: string = window.location.search): string | null {
  const params = new URLSearchParams(search);
  return params.get(RUN_QUERY_PARAM);
}

export function normalizeRuns(manifest: DashboardManifest | null): ManifestRun[] {
  const rows = Array.isArray(manifest?.runs) ? manifest.runs : [];
  return rows
    .filter((row): row is ManifestRun => Boolean(row && typeof row.id === "string" && row.id.trim()))
    .map((row) => ({
      ...row,
      label: String(row.label || row.id).trim(),
      artifacts: typeof row.artifacts === "object" && row.artifacts ? row.artifacts : {},
    }));
}

export function resolveReplayStudioLogPath(run: ManifestRun): string | null {
  const artifactPath = String(run.artifacts?.["replay_studio_log.json"] ?? "").trim();
  if (artifactPath) return artifactPath;
  const outputDir = String(run.output_dir ?? "").trim();
  return outputDir ? `${outputDir}\\replay_studio_log.json` : null;
}

