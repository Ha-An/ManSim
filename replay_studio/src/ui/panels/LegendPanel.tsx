export function LegendPanel() {
  return (
    <section className="panel-card">
      <h3>Legend</h3>
      <div className="legend-grid">
        <div><span className="legend-swatch info" /> Info / message flow</div>
        <div><span className="legend-swatch warning" /> Warning / bottleneck</div>
        <div><span className="legend-swatch error" /> Error / deadlock</div>
        <div><span className="legend-swatch machine" /> Machine</div>
        <div><span className="legend-swatch worker" /> Worker</div>
        <div><span className="legend-swatch queue" /> Queue / storage</div>
        <div><span className="legend-swatch charger" /> Charger</div>
      </div>
    </section>
  );
}
