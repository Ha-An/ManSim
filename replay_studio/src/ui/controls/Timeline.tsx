interface TimelineProps {
  currentTime: number;
  totalDuration: number;
  onSeek: (timestamp: number) => void;
  matchingEventCount: number;
}

export function Timeline({ currentTime, totalDuration, onSeek, matchingEventCount }: TimelineProps) {
  return (
    <div className="timeline-shell">
      <div className="timeline-header">
        <span>Replay Timeline</span>
        <span>{currentTime.toFixed(2)} / {totalDuration.toFixed(2)}</span>
      </div>
      <input
        className="timeline-input"
        type="range"
        min={0}
        max={totalDuration}
        step={0.01}
        value={Math.min(totalDuration, currentTime)}
        onChange={(event) => onSeek(Number(event.target.value))}
      />
      <div className="timeline-meta">
        <span>{matchingEventCount} matching events</span>
      </div>
    </div>
  );
}
