import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createReplayEngine, type ReplayEngine } from "./replay-core/replay/replayEngine";
import { parseReplayLog } from "./replay-core/parser/parseReplayLog";
import type { BaseEntityState } from "./replay-core/types/entity";
import type { ReplayFrameState, ReplayLog } from "./replay-core/types/replay";
import { FactoryScene3D } from "./scene/FactoryScene3D";
import { EntityInspector } from "./ui/EntityInspector";
import {
  getRequestedLogPath,
  getRequestedManifestPath,
  getRequestedRunId,
  normalizeRuns,
  resolveReplayStudioLogPath,
  toFetchablePath,
  type DashboardManifest,
  type ManifestRun,
} from "./routes";

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8] as const;
type ReplaySpeed = (typeof SPEEDS)[number];

function formatTime(value: number, unit: string): string {
  const suffix = unit === "seconds" ? "s" : "min";
  return `${value.toFixed(1)} ${suffix}`;
}

async function loadJson(path: string): Promise<unknown> {
  const fetchPath = toFetchablePath(path);
  if (!fetchPath) throw new Error("Path is missing.");
  const response = await fetch(fetchPath);
  if (!response.ok) throw new Error(`Failed to load ${path}: ${response.status}`);
  return response.json();
}

export default function App() {
  const engineRef = useRef<ReplayEngine>(createReplayEngine());
  const [log, setLog] = useState<ReplayLog | null>(null);
  const [frame, setFrame] = useState<ReplayFrameState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState<ReplaySpeed>(1);
  const [selectedEntityId, setSelectedEntityId] = useState<string | undefined>();
  const [runs, setRuns] = useState<ManifestRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");

  const selectedEntity = useMemo<BaseEntityState | undefined>(() => {
    if (!selectedEntityId || !frame) return undefined;
    return frame.domainState.entities[selectedEntityId];
  }, [frame, selectedEntityId]);

  const loadReplayLog = useCallback(async (rawPath: string) => {
    const payload = await loadJson(rawPath);
    const parsed = parseReplayLog(payload);
    engineRef.current.pause();
    engineRef.current.load(parsed);
    setLog(parsed);
    setFrame(engineRef.current.getCurrentState());
    setSelectedEntityId(undefined);
    setIsPlaying(false);
    setError(null);
  }, []);

  const loadFromManifest = useCallback(
    async (run: ManifestRun) => {
      const path = resolveReplayStudioLogPath(run);
      if (!path) throw new Error(`Run ${run.id} does not expose replay_studio_log.json.`);
      await loadReplayLog(path);
      setSelectedRunId(run.id);
    },
    [loadReplayLog],
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
          const manifest = (await loadJson(manifestPath)) as DashboardManifest;
          if (cancelled) return;
          const normalizedRuns = normalizeRuns(manifest);
          setRuns(normalizedRuns);
          const requestedRunId = getRequestedRunId();
          const selected =
            normalizedRuns.find((run) => run.id === requestedRunId) ??
            normalizedRuns.find((run) => run.id === String(manifest.current_run ?? "").trim()) ??
            normalizedRuns[normalizedRuns.length - 1];
          if (!selected) throw new Error("Dashboard manifest does not contain any runs.");
          await loadFromManifest(selected);
          return;
        }

        setRuns([]);
        setSelectedRunId("");
        await loadReplayLog(getRequestedLogPath());
      } catch (loadError) {
        if (!cancelled) setError(loadError instanceof Error ? loadError.message : String(loadError));
      }
    }

    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [loadFromManifest, loadReplayLog]);

  const totalDuration = log?.metadata.total_duration ?? 0;
  const currentTime = frame?.time ?? 0;
  const viewport = log?.layout?.viewport ?? { width: 1600, height: 960 };

  const togglePlay = useCallback(() => {
    if (!log) return;
    if (isPlaying) {
      engineRef.current.pause();
      setIsPlaying(false);
      return;
    }
    engineRef.current.play();
    setIsPlaying(true);
  }, [isPlaying, log]);

  const seek = useCallback((nextTime: number) => {
    if (!log) return;
    engineRef.current.pause();
    engineRef.current.seek(Math.max(0, Math.min(totalDuration, nextTime)));
    setIsPlaying(false);
  }, [log, totalDuration]);

  const changeSpeed = useCallback((nextSpeed: ReplaySpeed) => {
    engineRef.current.setSpeed(nextSpeed);
    setSpeed(nextSpeed);
  }, []);

  const selectedRun = runs.find((run) => run.id === selectedRunId);

  return (
    <main className="app-shell">
      <section className="scene-panel" aria-label="3D replay scene">
        {frame && log ? (
          <FactoryScene3D
            renderModel={frame.renderModel}
            currentEvent={frame.currentEvent}
            currentTime={frame.time}
            viewport={viewport}
            selectedEntityId={selectedEntityId}
            onSelectEntity={setSelectedEntityId}
          />
        ) : (
          <div className="loading-state">{error ? "Unable to load replay." : "Loading replay..."}</div>
        )}
      </section>

      <header className="top-hud">
        <div>
          <div className="hud-title">Replay Studio 3D</div>
          <div className="hud-subtitle">
            {log ? `${log.metadata.title ?? log.metadata.run_id} / ${formatTime(currentTime, log.metadata.time_unit)}` : "Waiting for replay log"}
          </div>
        </div>
        <div className="hud-row">
          {runs.length > 0 && (
            <select
              value={selectedRunId}
              onChange={(event) => {
                const run = runs.find((candidate) => candidate.id === event.target.value);
                if (run) void loadFromManifest(run);
              }}
              aria-label="Select run"
            >
              {runs.map((run) => (
                <option key={run.id} value={run.id}>
                  {run.label}
                </option>
              ))}
            </select>
          )}
          <button type="button" onClick={() => setSelectedEntityId(undefined)}>
            Clear Selection
          </button>
        </div>
      </header>

      <EntityInspector entity={selectedEntity} currentTime={currentTime} selectedRun={selectedRun} />

      <footer className="playback-bar">
        <button type="button" onClick={togglePlay} disabled={!log}>
          {isPlaying ? "Pause" : "Play"}
        </button>
        <input
          aria-label="Replay timeline"
          type="range"
          min={0}
          max={totalDuration || 1}
          step={0.1}
          value={currentTime}
          onChange={(event) => seek(Number(event.target.value))}
          disabled={!log}
        />
        <span className="time-readout">
          {log ? `${formatTime(currentTime, log.metadata.time_unit)} / ${formatTime(totalDuration, log.metadata.time_unit)}` : "--"}
        </span>
        <select value={speed} onChange={(event) => changeSpeed(Number(event.target.value) as ReplaySpeed)} aria-label="Playback speed">
          {SPEEDS.map((value) => (
            <option key={value} value={value}>
              {value}x
            </option>
          ))}
        </select>
      </footer>

      {error && <div className="error-banner">{error}</div>}
    </main>
  );
}
