interface JumpControlsProps {
  onJumpFiltered: () => void;
  onJumpWarning: () => void;
}

export function JumpControls({ onJumpFiltered, onJumpWarning }: JumpControlsProps) {
  return (
    <div className="control-group">
      <button className="ui-button" type="button" onClick={onJumpFiltered}>
        Next Match
      </button>
      <button className="ui-button danger" type="button" onClick={onJumpWarning}>
        Next Warning
      </button>
    </div>
  );
}
