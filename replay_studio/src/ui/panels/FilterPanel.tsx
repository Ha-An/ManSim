import type { EntityType } from "../../core/types/entity";
import type { ReplayEventType } from "../../core/types/event";

interface FilterPanelProps {
  eventTypes: ReplayEventType[];
  entityTypes: EntityType[];
  selectedEventType: string;
  entityTypeFilter: string;
  entityIdFilter: string;
  searchQuery: string;
  followSelectedEntity: boolean;
  onSelectedEventTypeChange: (value: string) => void;
  onEntityTypeChange: (value: string) => void;
  onEntityIdChange: (value: string) => void;
  onSearchChange: (value: string) => void;
  onFollowSelectedChange: (value: boolean) => void;
  onReset: () => void;
}

export function FilterPanel(props: FilterPanelProps) {
  return (
    <section className="panel-card">
      <h3>Filters</h3>
      <label className="panel-field">
        <span>Search</span>
        <input className="ui-input" value={props.searchQuery} onChange={(event) => props.onSearchChange(event.target.value)} />
      </label>
      <label className="panel-field">
        <span>Event Type</span>
        <select className="ui-select" value={props.selectedEventType} onChange={(event) => props.onSelectedEventTypeChange(event.target.value)}>
          <option value="">All</option>
          {props.eventTypes.map((eventType) => (
            <option key={eventType} value={eventType}>
              {eventType}
            </option>
          ))}
        </select>
      </label>
      <label className="panel-field">
        <span>Entity Type</span>
        <select className="ui-select" value={props.entityTypeFilter} onChange={(event) => props.onEntityTypeChange(event.target.value)}>
          <option value="">All</option>
          {props.entityTypes.map((entityType) => (
            <option key={entityType} value={entityType}>
              {entityType}
            </option>
          ))}
        </select>
      </label>
      <label className="panel-field">
        <span>Entity ID</span>
        <input className="ui-input" value={props.entityIdFilter} onChange={(event) => props.onEntityIdChange(event.target.value)} />
      </label>
      <label className="panel-check">
        <input type="checkbox" checked={props.followSelectedEntity} onChange={(event) => props.onFollowSelectedChange(event.target.checked)} />
        <span>Dim non-selected entities</span>
      </label>
      <button className="ui-button" type="button" onClick={props.onReset}>
        Reset Filters
      </button>
    </section>
  );
}
