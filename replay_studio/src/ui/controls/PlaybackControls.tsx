interface PlaybackControlsProps {
  isPlaying: boolean;
  onPlayPause: () => void;
  onReset: () => void;
}

export function PlaybackControls({ isPlaying, onPlayPause, onReset }: PlaybackControlsProps) {
  return (
    <div className="control-group">
      <button className="ui-button primary" type="button" onClick={onPlayPause}>
        {isPlaying ? "Pause" : "Play"}
      </button>
      <button className="ui-button" type="button" onClick={onReset}>
        Reset
      </button>
    </div>
  );
}
