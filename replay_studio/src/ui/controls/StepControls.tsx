interface StepControlsProps {
  onStepBackward: () => void;
  onStepForward: () => void;
}

export function StepControls({ onStepBackward, onStepForward }: StepControlsProps) {
  return (
    <div className="control-group">
      <button className="ui-button" type="button" onClick={onStepBackward}>
        Step -
      </button>
      <button className="ui-button" type="button" onClick={onStepForward}>
        Step +
      </button>
    </div>
  );
}
