export const DEFAULT_DEMO_LOG_PATH = "/demo/mansim_existing_run_log.json";
export const LOG_QUERY_PARAM = "log";
export const MANIFEST_QUERY_PARAM = "manifest";
export const RUN_QUERY_PARAM = "run";
export const VIEW_QUERY_PARAM = "view";

export type ReplayStudioView = "factory" | "manager";

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

export function getRequestedView(search: string = window.location.search): ReplayStudioView {
  const params = new URLSearchParams(search);
  const requested = String(params.get(VIEW_QUERY_PARAM) || "").trim().toLowerCase();
  return requested === "manager" ? "manager" : "factory";
}
