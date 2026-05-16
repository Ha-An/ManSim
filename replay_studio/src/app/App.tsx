import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { demoSeed } from "../demo/demoSeed";
import { buildRenderModel } from "../core/render-model/buildRenderModel";
import { createReplayEngine, type ReplayEngine } from "../core/replay/replayEngine";
import type { EntityType } from "../core/types/entity";
import type { ReplayFrameState, ReplayLog } from "../core/types/replay";
import {
  getRequestedView,
  getRequestedLogPath,
  getRequestedManifestPath,
  getRequestedRunId,
  MANIFEST_QUERY_PARAM,
  RUN_QUERY_PARAM,
  VIEW_QUERY_PARAM,
  toFetchablePath,
} from "./routes";
import { SceneLayer } from "../renderer/scene/SceneLayer";
import { PlaybackControls } from "../ui/controls/PlaybackControls";
import { SpeedControl } from "../ui/controls/SpeedControl";
import { StepControls } from "../ui/controls/StepControls";
import { JumpControls } from "../ui/controls/JumpControls";
import { Timeline } from "../ui/controls/Timeline";
import { EntityMonitorPanel } from "../ui/panels/EntityMonitorPanel";
import { useUIStore } from "../ui/state/uiStore";
import { ManagerReplayView, type ManagerReplayPayload } from "./ManagerReplayView";

type ManifestRun = {
  id: string;
  label: string;
  output_dir?: string;
  artifacts?: Record<string, string>;
};

type DashboardManifest = {
  current_run?: string;
  runs?: ManifestRun[];
};

function matchesEvent(
  event: ReplayLog["events"][number],
  log: ReplayLog,
  selectedEventType: string,
  entityTypeFilter: string,
  entityIdFilter: string,
  searchQuery: string,
) {
  if (selectedEventType && event.event_type !== selectedEventType) return false;
  if (entityIdFilter) {
    const refs = [event.entity_refs.primary, event.entity_refs.source, event.entity_refs.target, ...(event.entity_refs.related ?? [])].filter(Boolean);
    if (!refs.includes(entityIdFilter)) return false;
  }

  if (entityTypeFilter) {
    const refs = [event.entity_refs.primary, event.entity_refs.source, event.entity_refs.target, ...(event.entity_refs.related ?? [])]
      .filter((value): value is string => Boolean(value))
      .map((entityId) => log.initial_state?.entities?.[entityId]?.entity_type ?? (entityId === event.entity_refs.primary ? (event.payload.entity_type as string | undefined) : undefined))
      .filter(Boolean);
    if (!refs.includes(entityTypeFilter as EntityType)) return false;
  }

  const query = searchQuery.trim().toLowerCase();
  if (!query) return true;
  return JSON.stringify(event).toLowerCase().includes(query);
}

function formatMeta(log: ReplayLog): string {
  return `${log.metadata.domain} / ${log.metadata.time_unit} / ${log.metadata.total_duration}`;
}

function normalizeRuns(manifest: DashboardManifest | null): ManifestRun[] {
  const rows = Array.isArray(manifest?.runs) ? manifest.runs : [];
  return rows
    .filter((row): row is ManifestRun => Boolean(row && typeof row.id === "string" && row.id.trim()))
    .map((row) => ({
      ...row,
      label: String(row.label || row.id).trim(),
      artifacts: typeof row.artifacts === "object" && row.artifacts ? row.artifacts : {},
    }));
}

function resolveReplayStudioLogPath(run: ManifestRun): string | null {
  const artifactPath = String(run.artifacts?.["replay_studio_log.json"] ?? "").trim();
  if (artifactPath) return artifactPath;
  const outputDir = String(run.output_dir ?? "").trim();
  if (!outputDir) return null;
  return `${outputDir}\\replay_studio_log.json`;
}

function resolveManagerReplayPath(run: ManifestRun): string | null {
  const artifactPath = String(run.artifacts?.["manager_replay.json"] ?? "").trim();
  if (artifactPath) return artifactPath;
  const outputDir = String(run.output_dir ?? "").trim();
  if (!outputDir) return null;
  return `${outputDir}\\manager_replay.json`;
}

function isManagerReplayPayload(value: unknown): value is ManagerReplayPayload {
  if (!value || typeof value !== "object") return false;
  const payload = value as Record<string, unknown>;
  return Array.isArray(payload.days) && Boolean(payload.meta && typeof payload.meta === "object");
}

function updateRunQuery(manifestPath: string | null, runId: string | null, view: "factory" | "manager"): void {
  const url = new URL(window.location.href);
  if (manifestPath) {
    url.searchParams.set(MANIFEST_QUERY_PARAM, manifestPath);
  } else {
    url.searchParams.delete(MANIFEST_QUERY_PARAM);
  }
  if (runId) {
    url.searchParams.set(RUN_QUERY_PARAM, runId);
  } else {
    url.searchParams.delete(RUN_QUERY_PARAM);
  }
  url.searchParams.delete("log");
  if (view === "manager") {
    url.searchParams.set(VIEW_QUERY_PARAM, "manager");
  } else {
    url.searchParams.delete(VIEW_QUERY_PARAM);
  }
  window.history.replaceState({}, "", url.toString());
}

export default function App() {
  const engineRef = useRef<ReplayEngine>(createReplayEngine());
  const [log, setLog] = useState<ReplayLog | null>(null);
  const [managerPayload, setManagerPayload] = useState<ManagerReplayPayload | null>(null);
  const [frame, setFrame] = useState<ReplayFrameState | null>(null);
  const [loadingError, setLoadingError] = useState<string | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState<0.25 | 0.5 | 1 | 2 | 4 | 8>(1);
  const [dragging, setDragging] = useState(false);
  const [manifestData, setManifestData] = useState<DashboardManifest | null>(null);
  const [availableRuns, setAvailableRuns] = useState<ManifestRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [requestedManifestPath, setRequestedManifestPath] = useState<string | null>(null);

  const {
    selectedEntityId,
    selectedEventType,
    entityTypeFilter,
    entityIdFilter,
    searchQuery,
    followSelectedEntity,
    setSelectedEntityId,
  } = useUIStore();
  const requestedView = getRequestedView();

  const loadReplayLog = useCallback(
    async (rawPath: string) => {
      const fetchPath = toFetchablePath(rawPath);
      if (!fetchPath) {
        throw new Error("Replay Studio log path is missing.");
      }
      const response = await fetch(fetchPath);
      if (!response.ok) {
        throw new Error(`Failed to load replay log: ${response.status}`);
      }
      const payload = await response.json();
      const { parseReplayLog } = await import("../core/parser/parseReplayLog");
      const parsed = parseReplayLog(payload);
      engineRef.current.pause();
      engineRef.current.load(parsed);
      setLog(parsed);
      setManagerPayload(null);
      setFrame(engineRef.current.getCurrentState());
      setLoadingError(null);
      setIsPlaying(false);
      setSelectedEntityId(undefined);
    },
    [setSelectedEntityId],
  );

  const loadManagerReplay = useCallback(async (rawPath: string) => {
    const fetchPath = toFetchablePath(rawPath);
    if (!fetchPath) {
      throw new Error("Manager replay payload path is missing.");
    }
    const response = await fetch(fetchPath);
    if (!response.ok) {
      throw new Error(`Failed to load manager replay payload: ${response.status}`);
    }
    const payload = (await response.json()) as unknown;
    if (!isManagerReplayPayload(payload)) {
      throw new Error("Manager replay payload is invalid.");
    }
    engineRef.current.pause();
    setLog(null);
    setManagerPayload(payload);
    setFrame(null);
    setLoadingError(null);
    setIsPlaying(false);
    setSelectedEntityId(undefined);
  }, [setSelectedEntityId]);

  const loadRunFromManifest = useCallback(
    async (run: ManifestRun, manifestPath: string | null, manifestPayload: DashboardManifest | null) => {
      setManifestData(manifestPayload);
      setAvailableRuns(normalizeRuns(manifestPayload));
      setSelectedRunId(run.id);
      setRequestedManifestPath(manifestPath);
      if (getRequestedView() === "manager") {
        const managerPath = resolveManagerReplayPath(run);
        if (!managerPath) {
          throw new Error(`Run ${run.id} does not expose manager_replay.json.`);
        }
        await loadManagerReplay(managerPath);
        updateRunQuery(manifestPath, run.id, "manager");
        return;
      }
      const logPath = resolveReplayStudioLogPath(run);
      if (!logPath) {
        throw new Error(`Run ${run.id} does not expose replay_studio_log.json.`);
      }
      await loadReplayLog(logPath);
      updateRunQuery(manifestPath, run.id, "factory");
    },
    [loadManagerReplay, loadReplayLog],
  );

  useEffect(() => {
    const unsubscribe = engineRef.current.subscribe(() => {
      setFrame(engineRef.current.getCurrentState());
    });
    return unsubscribe;
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      try {
        const manifestPath = getRequestedManifestPath();
        if (manifestPath) {
          const manifestFetchPath = toFetchablePath(manifestPath);
          if (!manifestFetchPath) {
            throw new Error("Manifest path is invalid.");
          }
          const manifestResponse = await fetch(manifestFetchPath);
          if (!manifestResponse.ok) {
            throw new Error(`Failed to load dashboard manifest: ${manifestResponse.status}`);
          }
          const payload = (await manifestResponse.json()) as DashboardManifest;
          if (cancelled) return;
          const runs = normalizeRuns(payload);
          if (!runs.length) {
            throw new Error("Dashboard manifest does not contain any runs.");
          }
          const requestedRunId = getRequestedRunId();
          const selectedRun =
            runs.find((run) => run.id === requestedRunId) ??
            runs.find((run) => run.id === String(payload.current_run ?? "").trim()) ??
            runs[runs.length - 1];
          await loadRunFromManifest(selectedRun, manifestPath, payload);
          return;
        }

        setManifestData(null);
        setAvailableRuns([]);
        setSelectedRunId("");
        setRequestedManifestPath(null);
        if (getRequestedView() === "manager") {
          await loadManagerReplay(getRequestedLogPath());
          return;
        }
        await loadReplayLog(getRequestedLogPath());
      } catch (error) {
        if (cancelled) return;
        setLoadingError(error instanceof Error ? error.message : String(error));
      }
    }

    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [loadReplayLog, loadRunFromManifest]);

  const currentFrame = frame ?? engineRef.current.getCurrentState();

  useEffect(() => {
    if (log && currentFrame.time >= log.metadata.total_duration) {
      setIsPlaying(false);
    }
  }, [currentFrame.time, log]);

  const filteredEventIndexes = useMemo(() => {
    if (!log) return [];
    return log.events
      .map((event, index) => ({ event, index }))
      .filter(({ event }) => matchesEvent(event, log, selectedEventType, entityTypeFilter, entityIdFilter, searchQuery))
      .map(({ index }) => index);
  }, [entityIdFilter, entityTypeFilter, log, searchQuery, selectedEventType]);

  const renderModel = useMemo(
    () =>
      buildRenderModel(currentFrame.domainState, currentFrame.time, {
        logLayout: log?.layout,
        currentEvent: currentFrame.currentEvent,
        selectedEntityId,
        followSelected: followSelectedEntity,
        visibleEntityTypes: entityTypeFilter ? [entityTypeFilter] : undefined,
        entityIdFilter: entityIdFilter || undefined,
        searchQuery,
      }),
    [currentFrame, entityIdFilter, entityTypeFilter, followSelectedEntity, log?.layout, searchQuery, selectedEntityId],
  );

  const workerEntities = useMemo(
    () =>
      Object.values(currentFrame.domainState.entities)
        .filter((entity) => entity.entity_type === "worker" || entity.entity_type === "robot" || entity.entity_type === "transporter")
        .sort((left, right) => left.label.localeCompare(right.label)),
    [currentFrame.domainState.entities],
  );

  const machineEntities = useMemo(
    () =>
      Object.values(currentFrame.domainState.entities)
        .filter((entity) => entity.entity_type === "machine" || entity.entity_type === "workstation")
        .sort((left, right) => left.label.localeCompare(right.label)),
    [currentFrame.domainState.entities],
  );

  const itemEntities = useMemo(
    () =>
      Object.values(currentFrame.domainState.entities)
        .filter((entity) => typeof entity.attributes.item_state === "string")
        .sort((left, right) => left.label.localeCompare(right.label)),
    [currentFrame.domainState.entities],
  );

  const runSelectionEnabled = availableRuns.length > 1;

  async function handleJsonFile(file: File) {
    try {
      const text = await file.text();
      const parsedJson = JSON.parse(text);
      if (isManagerReplayPayload(parsedJson)) {
        engineRef.current.pause();
        setLog(null);
        setManagerPayload(parsedJson);
        setFrame(null);
      } else {
        const { parseReplayLog } = await import("../core/parser/parseReplayLog");
        const parsed = parseReplayLog(parsedJson);
        engineRef.current.pause();
        engineRef.current.load(parsed);
        setLog(parsed);
        setManagerPayload(null);
        setFrame(engineRef.current.getCurrentState());
      }
      setLoadingError(null);
      setIsPlaying(false);
      setSelectedEntityId(undefined);
      setManifestData(null);
      setAvailableRuns([]);
      setSelectedRunId("");
      setRequestedManifestPath(null);
    } catch (error) {
      setLoadingError(error instanceof Error ? error.message : String(error));
    }
  }

  const managerDescription = managerPayload
    ? `${managerPayload.meta.mode} / ${managerPayload.meta.model} / ${managerPayload.meta.total_days} days / ${managerPayload.meta.minutes_per_day} minutes per day`
    : "Phase-aligned manager orchestration replay.";

  if (requestedView === "manager") {
    return (
      <div
        className={`app-shell ${dragging ? "dragging" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={async (event) => {
          event.preventDefault();
          setDragging(false);
          const file = event.dataTransfer.files[0];
          if (!file) return;
          await handleJsonFile(file);
        }}
      >
        <header className="topbar">
          <div>
            <div className="eyebrow">Manager Replay</div>
            <h1>{managerPayload?.meta.run_id ? `Manager Replay / ${managerPayload.meta.run_id}` : "Manager Replay"}</h1>
            <p>{managerDescription}</p>
          </div>
          {runSelectionEnabled ? (
            <div className="topbar-run-selector">
              <label className="control-inline">
                <span>Run</span>
                <select
                  className="ui-select"
                  value={selectedRunId}
                  onChange={(event) => {
                    const nextRun = availableRuns.find((run) => run.id === event.target.value);
                    if (!nextRun) return;
                    void loadRunFromManifest(nextRun, requestedManifestPath, manifestData);
                  }}
                >
                  {availableRuns.map((run) => (
                    <option key={run.id} value={run.id}>
                      {run.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          ) : null}
        </header>
        {loadingError ? <div className="error-banner">{loadingError}</div> : null}
        {managerPayload ? (
          <ManagerReplayView payload={managerPayload} />
        ) : (
          <div className="panel-card manager-empty-state">Manager replay payload is not loaded.</div>
        )}
      </div>
    );
  }

  return (
    <div
      className={`app-shell ${dragging ? "dragging" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={async (event) => {
        event.preventDefault();
        setDragging(false);
        const file = event.dataTransfer.files[0];
        if (!file) return;
        await handleJsonFile(file);
      }}
    >
      <header className="topbar">
        <div>
          <div className="eyebrow">Replay Studio</div>
          <h1>{log?.metadata.title ?? demoSeed.title}</h1>
          <p>{log ? formatMeta(log) : demoSeed.description}</p>
        </div>
        {runSelectionEnabled ? (
          <div className="topbar-run-selector">
            <label className="control-inline">
              <span>Run</span>
              <select
                className="ui-select"
                value={selectedRunId}
                onChange={(event) => {
                  const nextRun = availableRuns.find((run) => run.id === event.target.value);
                  if (!nextRun) return;
                  void loadRunFromManifest(nextRun, requestedManifestPath, manifestData);
                }}
              >
                {availableRuns.map((run) => (
                  <option key={run.id} value={run.id}>
                    {run.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        ) : null}
      </header>

      {loadingError ? <div className="error-banner">{loadingError}</div> : null}

      <main className="content-grid">
        <section className="scene-panel">
          <div className="control-row">
            <PlaybackControls
              isPlaying={isPlaying}
              onPlayPause={() => {
                if (!log) return;
                if (isPlaying) {
                  engineRef.current.pause();
                  setIsPlaying(false);
                } else {
                  engineRef.current.play();
                  setIsPlaying(true);
                }
              }}
              onReset={() => {
                if (!log) return;
                engineRef.current.pause();
                engineRef.current.seek(0);
                setIsPlaying(false);
              }}
            />
            <SpeedControl
              value={speed}
              onChange={(nextSpeed) => {
                engineRef.current.setSpeed(nextSpeed);
                setSpeed(nextSpeed);
              }}
            />
            <StepControls
              onStepBackward={() => {
                if (!log) return;
                engineRef.current.pause();
                engineRef.current.stepBackward();
                setIsPlaying(false);
              }}
              onStepForward={() => {
                if (!log) return;
                engineRef.current.pause();
                engineRef.current.stepForward();
                setIsPlaying(false);
              }}
            />
            <JumpControls
              onJumpFiltered={() => {
                if (!log) return;
                engineRef.current.pause();
                engineRef.current.jumpToNextEvent((event) => matchesEvent(event, log, selectedEventType, entityTypeFilter, entityIdFilter, searchQuery));
                setIsPlaying(false);
              }}
              onJumpWarning={() => {
                engineRef.current.pause();
                engineRef.current.jumpToNextWarning();
                setIsPlaying(false);
              }}
            />
          </div>

          <div className="scene-header">
            <div className="scene-title">Scene</div>
            <div className="scene-meta">
              <span>{log?.metadata.run_id ?? "no-log"}</span>
              <span>{currentFrame.time.toFixed(2)} {log?.metadata.time_unit ?? "minutes"}</span>
            </div>
          </div>

          <Timeline
            currentTime={currentFrame.time}
            totalDuration={log?.metadata.total_duration ?? 1}
            matchingEventCount={filteredEventIndexes.length}
            onSeek={(time) => {
              engineRef.current.pause();
              engineRef.current.seek(time);
              setIsPlaying(false);
            }}
          />

          <SceneLayer
            renderModel={renderModel}
            currentEvent={currentFrame.currentEvent}
            currentTime={currentFrame.time}
            viewport={log?.layout?.viewport ?? { width: 1200, height: 760 }}
            onSelectEntity={(entityId) => setSelectedEntityId(entityId)}
          />
        </section>

        <aside className="side-panel">
          <EntityMonitorPanel
            workers={workerEntities}
            machines={machineEntities}
            items={itemEntities}
            regions={renderModel.regions}
            currentTime={currentFrame.time}
            grid={log?.layout?.grid}
            viewport={log?.layout?.viewport ?? { width: 1200, height: 760 }}
          />
        </aside>
      </main>
    </div>
  );
}
