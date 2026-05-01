import type { ReplayEvent } from "../../core/types/event";

interface EventInspectorProps {
  event?: ReplayEvent;
}

export function EventInspector({ event }: EventInspectorProps) {
  return (
    <section className="panel-card">
      <h3>Event Inspector</h3>
      {event ? (
        <>
          <div className="panel-kv"><span>ID</span><span>{event.event_id}</span></div>
          <div className="panel-kv"><span>Type</span><span>{event.event_type}</span></div>
          <div className="panel-kv"><span>Timestamp</span><span>{event.timestamp.toFixed(2)}</span></div>
          <pre className="panel-pre">{JSON.stringify(event, null, 2)}</pre>
        </>
      ) : (
        <p className="muted">No current event.</p>
      )}
    </section>
  );
}
