import type { BaseEntityState } from "../../core/types/entity";

interface EntityInspectorProps {
  entity?: BaseEntityState;
}

export function EntityInspector({ entity }: EntityInspectorProps) {
  return (
    <section className="panel-card">
      <h3>Entity Inspector</h3>
      {entity ? (
        <>
          <div className="panel-kv"><span>ID</span><span>{entity.entity_id}</span></div>
          <div className="panel-kv"><span>Type</span><span>{entity.entity_type}</span></div>
          <div className="panel-kv"><span>State</span><span>{entity.state}</span></div>
          <pre className="panel-pre">{JSON.stringify(entity, null, 2)}</pre>
        </>
      ) : (
        <p className="muted">Select an entity in the scene.</p>
      )}
    </section>
  );
}
