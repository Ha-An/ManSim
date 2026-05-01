interface SpeedControlProps {
  value: 0.25 | 0.5 | 1 | 2 | 4 | 8;
  onChange: (speed: 0.25 | 0.5 | 1 | 2 | 4 | 8) => void;
}

const options: Array<0.25 | 0.5 | 1 | 2 | 4 | 8> = [0.25, 0.5, 1, 2, 4, 8];

export function SpeedControl({ value, onChange }: SpeedControlProps) {
  return (
    <label className="control-inline">
      <span>Speed</span>
      <select className="ui-select" value={value} onChange={(event) => onChange(Number(event.target.value) as 0.25 | 0.5 | 1 | 2 | 4 | 8)}>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}x
          </option>
        ))}
      </select>
    </label>
  );
}
