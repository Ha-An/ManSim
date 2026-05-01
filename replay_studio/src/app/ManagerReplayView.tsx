import { useEffect, useMemo, useState } from "react";

type ManagerPhaseType = "strategist" | "compiler" | "reviewer";

export type ManagerReplayPayload = {
  meta: {
    run_id: string;
    mode: string;
    model: string;
    total_days: number;
    minutes_per_day: number;
    total_products: number;
    closure_ratio: number;
    wall_clock_human: string;
  };
  assets?: {
    manager_sprite?: string;
  };
  days: ManagerDay[];
  factory_snapshots?: Record<string, unknown>;
};

type ManagerDay = {
  day: number;
  phases: ManagerPhase[];
  day_kpis?: Record<string, unknown>;
  day_summary?: Record<string, unknown> | string[] | string;
  scene?: {
    regions?: SceneRegion[];
  };
};

type ManagerPhase = {
  id: string;
  phase_type: ManagerPhaseType;
  actor_id: string;
  actor_label: string;
  started_at: string;
  ended_at: string;
  time_label: string;
  inputs_summary?: string[];
  decision_summary?: string;
  decision_structured?: Record<string, unknown>;
  excerpt?: string;
  why_changed?: string[];
  carry_forward_risks?: string[];
  factory_effect?: {
    summary_lines?: string[];
    highlights?: string[];
    incidents?: Record<string, number>;
  };
};

type SceneRegion = {
  id: string;
  label: string;
  kpi?: string;
  metrics?: RegionMetric[];
  machines?: Array<string | RegionMachine>;
  roles?: Array<string | RegionRole>;
};

type RegionMetric = {
  label: string;
  start_label?: string;
  end_label?: string;
  start?: number;
  end?: number;
  delta?: number;
};

type RegionMachine = {
  id: string;
  start_state?: string;
  end_state?: string;
  broken_min?: number;
};

type RegionRole = {
  worker: string;
  role: string;
  changed?: boolean;
};

type SignalBadgeTone = "queue" | "focus" | "role" | "support" | "risk" | "default";

type SignalBadge = {
  key: string;
  label: string;
  detail: string;
  tone: SignalBadgeTone;
};

type FactoryDiffCard = {
  key: string;
  region: string;
  metric: string;
  before: string;
  after: string;
  delta: number;
};

type StageSummaryItem = {
  label: string;
  value: string;
};

type RegionSnapshot = {
  region: string;
  highlighted: boolean;
  metrics: FactoryDiffCard[];
};

function phaseTone(phaseType: ManagerPhaseType): string {
  switch (phaseType) {
    case "strategist":
      return "strategist";
    case "compiler":
      return "compiler";
    case "reviewer":
      return "reviewer";
    default:
      return "strategist";
  }
}

function phaseLabel(phaseType: ManagerPhaseType): string {
  switch (phaseType) {
    case "strategist":
      return "Strategist";
    case "compiler":
      return "Compiler";
    case "reviewer":
      return "Reviewer";
    default:
      return phaseType;
  }
}

function toPhrase(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (token) => token.toUpperCase());
}

function formatScalar(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isInteger(value) ? `${value}` : value.toFixed(3);
  return String(value);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function asStringArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((entry) => formatScalar(entry));
  if (typeof value === "string") return [value];
  return [];
}

function getDaySummaryRecord(day: ManagerDay | undefined): Record<string, unknown> {
  return asRecord(day?.day_summary) ?? {};
}

function getPhase(day: ManagerDay | undefined, phaseType: ManagerPhaseType): ManagerPhase | undefined {
  return day?.phases.find((phase) => phase.phase_type === phaseType);
}

function classifySignal(entry: string): Pick<SignalBadge, "label" | "tone"> {
  const normalized = entry.toLowerCase();
  if (normalized.includes("queue") || normalized.includes("buffer")) return { label: "Queue Pressure", tone: "queue" };
  if (normalized.includes("focus")) return { label: "Focus Hint", tone: "focus" };
  if (normalized.includes("role") || normalized.includes("worker")) return { label: "Role Constraint", tone: "role" };
  if (normalized.includes("support")) return { label: "Support Signal", tone: "support" };
  if (
    normalized.includes("breakdown") ||
    normalized.includes("risk") ||
    normalized.includes("close-out") ||
    normalized.includes("closeout") ||
    normalized.includes("underfeed")
  ) {
    return { label: "Risk Signal", tone: "risk" };
  }
  return { label: "Input Signal", tone: "default" };
}

function uniqueLines(lines: string[]): string[] {
  return Array.from(new Set(lines.map((line) => line.trim()).filter(Boolean)));
}

function buildInputBundle(day: ManagerDay, phase: ManagerPhase | undefined, previousDay: ManagerDay | undefined): SignalBadge[] {
  const previousSummary = getDaySummaryRecord(previousDay);
  const lines = uniqueLines([
    ...asStringArray(phase?.inputs_summary),
    ...asStringArray(previousSummary.carry_forward_risks),
    ...asStringArray(previousSummary.next_day_prevention_hints),
    ...asStringArray(previousSummary.policy_critique_hints),
  ]);

  return lines.map((entry, index) => {
    const classified = classifySignal(entry);
    return {
      key: `${day.day}-${phase?.id ?? "phase"}-${index}`,
      label: classified.label,
      detail: entry,
      tone: classified.tone,
    };
  });
}

function summarizeRolePlan(value: unknown): string {
  const record = asRecord(value);
  if (!record) return formatScalar(value);
  return Object.entries(record)
    .map(([worker, nested]) => {
      const nestedRecord = asRecord(nested);
      const role = typeof nested === "string" ? nested : formatScalar(nestedRecord?.role ?? nested);
      return `${worker} ${role}`;
    })
    .join("\n");
}

function summarizeSupportPlan(value: unknown): string {
  const record = asRecord(value);
  if (!record) return formatScalar(value);
  const pair = typeof record.primary_support_pair === "string" ? record.primary_support_pair : null;
  const intent = typeof record.support_intent === "string" ? record.support_intent : null;
  const reason = typeof record.reason === "string" ? record.reason : null;
  return [
    pair ? `Primary Pair: ${pair}` : null,
    intent ? `Intent: ${intent}` : null,
    reason ? `Reason: ${reason}` : null,
  ]
    .filter(Boolean)
    .join("\n");
}

function summarizeTargets(value: unknown): string {
  return asStringArray(value)
    .map((entry) => entry.replace(/_/g, " "))
    .join("\n");
}

function summarizeDailyTargets(value: unknown): string {
  const record = asRecord(value);
  if (!record) return formatScalar(value);
  return Object.entries(record)
    .map(([key, nested]) => `${key.replace(/_/g, " ")}=${formatScalar(nested)}`)
    .join("\n");
}

function summarizeWorkerRoles(value: unknown): string {
  const record = asRecord(value);
  if (!record) return formatScalar(value);
  return Object.entries(record)
    .map(([worker, role]) => `${worker} ${formatScalar(role)}`)
    .join("\n");
}

function extractRolePlanRoles(value: unknown): Record<string, string> {
  const record = asRecord(value);
  if (!record) return {};
  const roles: Record<string, string> = {};
  for (const [worker, nested] of Object.entries(record)) {
    const nestedRecord = asRecord(nested);
    const role = typeof nested === "string" ? nested : nestedRecord?.role;
    if (role !== undefined && role !== null && `${role}`.trim()) {
      roles[worker] = `${role}`.trim();
    }
  }
  return roles;
}

function legacyRoleChangeReason(compiledRole: string): string {
  const role = compiledRole.toLowerCase();
  if (role === "inspection_closer") return "ensured required inspection coverage";
  if (role === "reliability_guard") return "legacy compiler stabilized support coverage as reliability guard";
  if (role === "battery_support") return "battery risk guardrail applied";
  if (role === "intake_runner") return "kept intake flow coverage";
  return "compiler role guardrail applied";
}

function summarizeRoleOverrideReasons(compilerStructured: Record<string, unknown>, strategistStructured: Record<string, unknown> | undefined): string {
  const explicitReasons = asRecord(compilerStructured.role_override_reasons);
  const compiledRoles = asRecord(compilerStructured.worker_roles);
  const strategistRoles = extractRolePlanRoles(strategistStructured?.role_plan);
  if (!compiledRoles || !Object.keys(strategistRoles).length) {
    return explicitReasons
      ? Object.entries(explicitReasons).map(([worker, reason]) => `${worker}: ${formatScalar(reason)}`).join("\n")
      : "";
  }

  return Object.entries(compiledRoles)
    .filter(([worker, role]) => {
      const before = strategistRoles[worker];
      return before && formatScalar(before) !== formatScalar(role);
    })
    .map(([worker, role]) => {
      const reason = explicitReasons?.[worker] ?? legacyRoleChangeReason(formatScalar(role));
      return `${worker} ${formatScalar(role)}: ${formatScalar(reason)}`;
    })
    .join("\n");
}

function summarizeWeights(value: unknown): string {
  const record = asRecord(value);
  if (!record) return formatScalar(value);
  return Object.entries(record)
    .sort((left, right) => Number(right[1] ?? 0) - Number(left[1] ?? 0))
    .slice(0, 4)
    .map(([task, weight]) => `${task.replace(/_/g, " ")} ${formatScalar(weight)}`)
    .join("\n");
}

function summarizeMultipliers(value: unknown): string {
  const record = asRecord(value);
  if (!record) return formatScalar(value);
  return Object.entries(record)
    .map(([worker, nested]) => {
      const nestedRecord = asRecord(nested);
      if (!nestedRecord) return `${worker} ${formatScalar(nested)}`;
      const [topTask, topValue] = Object.entries(nestedRecord).sort((left, right) => Number(right[1] ?? 0) - Number(left[1] ?? 0))[0] ?? [];
      return topTask ? `${worker} ${topTask.replace(/_/g, " ")} ${formatScalar(topValue)}` : worker;
    })
    .join("\n");
}

function summarizeMailboxSeed(value: unknown): string {
  const record = asRecord(value);
  if (!record) return formatScalar(value);
  return Object.entries(record)
    .map(([worker, messages]) => `${worker} ${Array.isArray(messages) ? messages.length : 0} msg`)
    .join("\n");
}

function summarizeReviewerPair(value: unknown): string {
  return formatScalar(value).replace(/_/g, " ");
}

function buildStageSummaryItems(phase: ManagerPhase | undefined, context?: { strategistPhase?: ManagerPhase }): StageSummaryItem[] {
  if (!phase) return [];
  const structured = phase.decision_structured ?? {};

  if (phase.phase_type === "strategist") {
    return [
      { label: "Operating Focus", value: formatScalar(structured.operating_focus) },
      { label: "Role Plan", value: summarizeRolePlan(structured.role_plan) },
      { label: "Support Plan", value: summarizeSupportPlan(structured.support_plan) },
      { label: "Prevention Targets", value: summarizeTargets(structured.prevention_targets) },
      { label: "Daily Targets", value: summarizeDailyTargets(structured.daily_targets) },
    ].filter((entry) => entry.value && entry.value !== "-");
  }

  if (phase.phase_type === "compiler") {
    const roleChanges = summarizeRoleOverrideReasons(structured, context?.strategistPhase?.decision_structured);
    return [
      { label: "Worker Roles", value: summarizeWorkerRoles(structured.worker_roles) },
      { label: "Role Changes", value: roleChanges },
      { label: "Priority Weights", value: summarizeWeights(structured.task_priority_weights) },
      { label: "Priority Multipliers", value: summarizeMultipliers(structured.agent_priority_multipliers) },
      { label: "Mailbox Seed", value: summarizeMailboxSeed(structured.mailbox_seed) },
      { label: "Plan Revision", value: formatScalar(structured.plan_revision) },
    ].filter((entry) => entry.value && entry.value !== "-");
  }

  return [
    { label: "Target Misses", value: summarizeTargets(structured.target_misses) },
    { label: "Top Failure Modes", value: summarizeTargets(structured.top_failure_modes) },
    { label: "Recommended Prevention", value: summarizeTargets(structured.recommended_prevention_targets) },
    { label: "Support Pair", value: summarizeReviewerPair(structured.recommended_support_pair) },
    { label: "Carry Risks", value: summarizeTargets(structured.carry_forward_risks) },
  ].filter((entry) => entry.value && entry.value !== "-");
}

function buildFactoryDiffCards(day: ManagerDay): FactoryDiffCard[] {
  const cards: FactoryDiffCard[] = [];
  for (const region of day.scene?.regions ?? []) {
    for (const metric of region.metrics ?? []) {
      cards.push({
        key: `${region.id}-${metric.label}`,
        region: region.label,
        metric: metric.label,
        before: formatScalar(metric.start),
        after: formatScalar(metric.end),
        delta: Number(metric.delta ?? 0),
      });
    }
  }

  return cards.sort((left, right) => Math.abs(right.delta) - Math.abs(left.delta));
}

function buildFactoryRegions(day: ManagerDay, selectedPhase: ManagerPhase | undefined): RegionSnapshot[] {
  const cards = buildFactoryDiffCards(day);
  const highlighted = new Set((selectedPhase?.factory_effect?.highlights ?? []).map((entry) => entry.toLowerCase()));
  const grouped = new Map<string, FactoryDiffCard[]>();

  for (const card of cards) {
    const bucket = grouped.get(card.region) ?? [];
    bucket.push(card);
    grouped.set(card.region, bucket);
  }

  return Array.from(grouped.entries())
    .map(([region, metrics]) => ({
      region,
      highlighted: highlighted.has(region.toLowerCase()),
      metrics: metrics.sort((left, right) => Math.abs(right.delta) - Math.abs(left.delta)).slice(0, 3),
    }))
    .sort((left, right) => Number(right.highlighted) - Number(left.highlighted));
}

function buildIncidentSummaries(phase: ManagerPhase | undefined): StageSummaryItem[] {
  const incidents = phase?.factory_effect?.incidents ?? {};
  return Object.entries(incidents)
    .filter(([, value]) => Number(value ?? 0) > 0)
    .sort((left, right) => Number(right[1] ?? 0) - Number(left[1] ?? 0))
    .slice(0, 4)
    .map(([key, value]) => ({
      label: toPhrase(key),
      value: formatScalar(value),
    }));
}

function extractCarryForward(phase: ManagerPhase | undefined): string[] {
  if (!phase) return [];
  if (Array.isArray(phase.carry_forward_risks) && phase.carry_forward_risks.length) return phase.carry_forward_risks;
  const structured = phase.decision_structured ?? {};
  return asStringArray(structured.carry_forward_risks);
}

function buildNextDayFeedback(day: ManagerDay, reviewerPhase: ManagerPhase | undefined, nextDay: ManagerDay | undefined): StageSummaryItem[] {
  const nextStrategist = getPhase(nextDay, "strategist");
  const currentCarry = uniqueLines([
    ...extractCarryForward(reviewerPhase),
    ...asStringArray(getDaySummaryRecord(day).carry_forward_risks),
  ]);
  const nextSeeds = uniqueLines(asStringArray(nextStrategist?.inputs_summary));

  const items: StageSummaryItem[] = [];
  if (currentCarry.length) items.push({ label: "Carry Risks", value: currentCarry.join(" · ") });
  if (nextSeeds.length) items.push({ label: "Next-Day Inputs", value: nextSeeds.join(" · ") });
  if (!items.length) items.push({ label: "Next-Day Inputs", value: "No next-day handoff available." });
  return items;
}

function renderStructuredValue(value: unknown, pathKey: string): JSX.Element {
  if (Array.isArray(value)) {
    if (!value.length) return <span className="muted">No items</span>;

    const primitiveOnly = value.every((entry) => !asRecord(entry) && !Array.isArray(entry));
    if (primitiveOnly) {
      return (
        <div className="manager-token-list">
          {value.map((entry, index) => (
            <span className="manager-token" key={`${pathKey}-${index}`}>
              {formatScalar(entry)}
            </span>
          ))}
        </div>
      );
    }

    return (
      <div className="manager-structured-stack">
        {value.map((entry, index) => (
          <div className="manager-structured-card" key={`${pathKey}-${index}`}>
            <div className="manager-structured-value">{renderStructuredValue(entry, `${pathKey}-${index}`)}</div>
          </div>
        ))}
      </div>
    );
  }

  const record = asRecord(value);
  if (record) {
    if (pathKey.endsWith("role_plan")) {
      return (
        <div className="manager-structured-stack">
          {Object.entries(record).map(([worker, nested]) => {
            const nestedRecord = asRecord(nested);
            return (
              <div className="manager-structured-card" key={`${pathKey}-${worker}`}>
                <div className="manager-structured-key">{worker}</div>
                <div className="manager-structured-value strong-text">{formatScalar(nestedRecord?.role ?? nested)}</div>
                {typeof nestedRecord?.reason === "string" ? <div className="manager-structured-subvalue">{nestedRecord.reason}</div> : null}
              </div>
            );
          })}
        </div>
      );
    }

    if (pathKey.endsWith("worker_roles")) {
      return (
        <div className="manager-structured-stack compact">
          {Object.entries(record).map(([worker, role]) => (
            <div className="manager-structured-card" key={`${pathKey}-${worker}`}>
              <div className="manager-structured-key">{worker}</div>
              <div className="manager-structured-value strong-text">{formatScalar(role)}</div>
            </div>
          ))}
        </div>
      );
    }

    if (pathKey.endsWith("task_priority_weights")) {
      return (
        <div className="manager-structured-stack compact">
          {Object.entries(record)
            .sort((left, right) => Number(right[1] ?? 0) - Number(left[1] ?? 0))
            .map(([task, weight]) => (
              <div className="manager-structured-card" key={`${pathKey}-${task}`}>
                <div className="manager-structured-key">{toPhrase(task)}</div>
                <div className="manager-structured-value strong-text">{formatScalar(weight)}</div>
              </div>
            ))}
        </div>
      );
    }

    if (pathKey.endsWith("agent_priority_multipliers")) {
      return (
        <div className="manager-structured-stack">
          {Object.entries(record).map(([worker, nested]) => {
            const nestedRecord = asRecord(nested) ?? {};
            return (
              <div className="manager-structured-card" key={`${pathKey}-${worker}`}>
                <div className="manager-structured-key">{worker}</div>
                <div className="manager-keyval-list">
                  {Object.entries(nestedRecord)
                    .sort((left, right) => Number(right[1] ?? 0) - Number(left[1] ?? 0))
                    .slice(0, 5)
                    .map(([task, weight]) => (
                      <div className="manager-keyval-row" key={`${pathKey}-${worker}-${task}`}>
                        <span>{toPhrase(task)}</span>
                        <strong>{formatScalar(weight)}</strong>
                      </div>
                    ))}
                </div>
              </div>
            );
          })}
        </div>
      );
    }

    if (pathKey.endsWith("mailbox_seed")) {
      return (
        <div className="manager-structured-stack">
          {Object.entries(record).map(([worker, nested]) => {
            const messages = Array.isArray(nested) ? nested : [];
            return (
              <div className="manager-structured-card" key={`${pathKey}-${worker}`}>
                <div className="manager-structured-key">{worker}</div>
                <div className="manager-structured-subvalue">{messages.length} queued assist message(s)</div>
                {messages.length ? (
                  <div className="manager-token-list">
                    {messages.map((message, index) => {
                      const messageRecord = asRecord(message) ?? {};
                      return (
                        <span className="manager-token" key={`${pathKey}-${worker}-${index}`}>
                          {formatScalar(messageRecord.task_family ?? messageRecord.message_type ?? `msg ${index + 1}`)}
                        </span>
                      );
                    })}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      );
    }

    const entries = Object.entries(record);
    const scalarOnly = entries.every(([, nested]) => !asRecord(nested) && !Array.isArray(nested));
    if (scalarOnly) {
      return (
        <div className="manager-keyval-list">
          {entries.map(([key, nested]) => (
            <div className="manager-keyval-row" key={`${pathKey}-${key}`}>
              <span>{toPhrase(key)}</span>
              <strong>{formatScalar(nested)}</strong>
            </div>
          ))}
        </div>
      );
    }

    return (
      <div className="manager-structured-stack">
        {entries.map(([key, nested]) => (
          <div className="manager-structured-card" key={`${pathKey}-${key}`}>
            <div className="manager-structured-key">{toPhrase(key)}</div>
            <div className="manager-structured-value">{renderStructuredValue(nested, `${pathKey}-${key}`)}</div>
          </div>
        ))}
      </div>
    );
  }

  return <span className="manager-structured-inline">{formatScalar(value)}</span>;
}

function buildRegionSnapshotLabel(region: RegionSnapshot): string {
  const strongest = region.metrics[0];
  if (!strongest) return region.region;
  return `${strongest.metric}: ${strongest.before} -> ${strongest.after} (${strongest.delta >= 0 ? "+" : ""}${formatScalar(strongest.delta)})`;
}

export function ManagerReplayView({ payload }: { payload: ManagerReplayPayload }) {
  const [selectedDayNumber, setSelectedDayNumber] = useState<number>(() => payload.days[0]?.day ?? 1);
  const [selectedPhaseId, setSelectedPhaseId] = useState<string>(() => payload.days[0]?.phases[0]?.id ?? "");
  const [expandedStructuredKeys, setExpandedStructuredKeys] = useState<string[]>([]);

  useEffect(() => {
    const firstDay = payload.days[0];
    if (!firstDay) return;
    setSelectedDayNumber((current) => (payload.days.some((day) => day.day === current) ? current : firstDay.day));
  }, [payload]);

  const selectedDay = useMemo(
    () => payload.days.find((day) => day.day === selectedDayNumber) ?? payload.days[0],
    [payload.days, selectedDayNumber],
  );

  useEffect(() => {
    const fallbackPhaseId = selectedDay?.phases?.[0]?.id ?? "";
    setSelectedPhaseId((current) => (selectedDay?.phases.some((phase) => phase.id === current) ? current : fallbackPhaseId));
  }, [selectedDay]);

  const selectedPhase = useMemo(
    () => selectedDay?.phases.find((phase) => phase.id === selectedPhaseId) ?? selectedDay?.phases?.[0],
    [selectedDay, selectedPhaseId],
  );

  const previousDay = useMemo(() => payload.days.find((day) => day.day === (selectedDay?.day ?? 0) - 1), [payload.days, selectedDay]);
  const nextDay = useMemo(() => payload.days.find((day) => day.day === (selectedDay?.day ?? 0) + 1), [payload.days, selectedDay]);

  const strategistPhase = useMemo(() => getPhase(selectedDay, "strategist"), [selectedDay]);
  const compilerPhase = useMemo(() => getPhase(selectedDay, "compiler"), [selectedDay]);
  const reviewerPhase = useMemo(() => getPhase(selectedDay, "reviewer"), [selectedDay]);

  const strategistSignals = useMemo(
    () => (selectedDay ? buildInputBundle(selectedDay, strategistPhase, previousDay) : []),
    [selectedDay, strategistPhase, previousDay],
  );
  const strategistOutputs = useMemo(() => buildStageSummaryItems(strategistPhase), [strategistPhase]);
  const compilerOutputs = useMemo(
    () => buildStageSummaryItems(compilerPhase, { strategistPhase }),
    [compilerPhase, strategistPhase],
  );
  const reviewerOutputs = useMemo(() => buildStageSummaryItems(reviewerPhase), [reviewerPhase]);
  const factoryRegions = useMemo(() => (selectedDay ? buildFactoryRegions(selectedDay, selectedPhase) : []), [selectedDay, selectedPhase]);
  const incidentSummaries = useMemo(() => buildIncidentSummaries(selectedPhase), [selectedPhase]);
  const feedbackItems = useMemo(() => (selectedDay ? buildNextDayFeedback(selectedDay, reviewerPhase, nextDay) : []), [selectedDay, reviewerPhase, nextDay]);
  const structuredEntries = useMemo(() => Object.entries(selectedPhase?.decision_structured ?? {}), [selectedPhase]);
  const selectedPhaseSignals = useMemo(
    () => (selectedDay ? buildInputBundle(selectedDay, selectedPhase, previousDay) : []),
    [selectedDay, selectedPhase, previousDay],
  );
  const selectedPhaseCarry = useMemo(() => extractCarryForward(selectedPhase), [selectedPhase]);

  useEffect(() => {
    setExpandedStructuredKeys(structuredEntries.length ? [structuredEntries[0][0]] : []);
  }, [selectedPhase?.id, structuredEntries]);

  if (!selectedDay || !selectedPhase) {
    return <div className="error-banner">Manager replay payload is missing day or phase data.</div>;
  }

  const toggleStructuredKey = (key: string) => {
    setExpandedStructuredKeys((current) => (current.includes(key) ? current.filter((item) => item !== key) : [...current, key]));
  };

  return (
    <main className="manager-shell manager-pipeline-shell">
      <section className="panel-card manager-header-panel">
        <div className="manager-header-top">
          <div>
            <div className="eyebrow">Manager Replay</div>
            <h1 className="manager-header-title">Day-Centered Decision Pipeline</h1>
          </div>
          <div className="manager-header-meta">
            <div className="manager-meta-chip">
              <span>Run</span>
              <strong>{payload.meta.run_id}</strong>
            </div>
            <div className="manager-meta-chip">
              <span>Mode</span>
              <strong>{payload.meta.mode}</strong>
            </div>
            <div className="manager-meta-chip">
              <span>Products</span>
              <strong>{payload.meta.total_products}</strong>
            </div>
            <div className="manager-meta-chip">
              <span>Closure</span>
              <strong>{payload.meta.closure_ratio.toFixed(3)}</strong>
            </div>
            <div className="manager-meta-chip">
              <span>Wall Clock</span>
              <strong>{payload.meta.wall_clock_human}</strong>
            </div>
          </div>
        </div>

        <div className="manager-header-controls">
          <label className="control-inline">
            <span>Day</span>
            <select className="ui-select" value={selectedDayNumber} onChange={(event) => setSelectedDayNumber(Number(event.target.value))}>
              {payload.days.map((day) => (
                <option key={day.day} value={day.day}>
                  Day {day.day}
                </option>
              ))}
            </select>
          </label>
          <div className="manager-phase-selector">
            {selectedDay.phases.map((phase) => (
              <button
                key={phase.id}
                type="button"
                className={`manager-phase-button ${phaseTone(phase.phase_type)} ${selectedPhase.id === phase.id ? "active" : ""}`}
                onClick={() => setSelectedPhaseId(phase.id)}
              >
                <span>{phaseLabel(phase.phase_type)}</span>
                <small>{phase.time_label || phase.actor_label}</small>
              </button>
            ))}
          </div>
          <div className={`manager-phase-pill ${phaseTone(selectedPhase.phase_type)}`}>{phaseLabel(selectedPhase.phase_type)}</div>
        </div>

      </section>

      <section className="manager-pipeline-layout">
        <section className="panel-card manager-pipeline-panel">
          <div className="manager-pane-head manager-pipeline-head">
            <div>
              <div className="eyebrow">Sequential Flow</div>
              <h2>How Day {selectedDay.day} Progressed</h2>
            </div>
            <div className="manager-subtle">Selected stage: {phaseLabel(selectedPhase.phase_type)}</div>
          </div>

          <div className="manager-pipeline-track">
            <article className={`manager-stage-card input ${selectedPhase.phase_type === "strategist" ? "linked" : ""}`}>
              <div className="manager-stage-head">
                <div>
                  <span className="manager-stage-kicker">1. Input Bundle</span>
                  <h3>Signals Routed to Strategist</h3>
                </div>
              </div>
              <div className="manager-signal-grid">
                {strategistSignals.length ? (
                  strategistSignals.map((signal) => (
                    <div key={signal.key} className={`manager-signal-card ${signal.tone}`}>
                      <div className="manager-signal-label">{signal.label}</div>
                      <div className="manager-signal-detail">{signal.detail}</div>
                    </div>
                  ))
                ) : (
                  <div className="manager-empty-state">No strategist inputs recorded.</div>
                )}
              </div>
            </article>

            <div className="manager-stage-arrow" aria-hidden="true">
              {"->"}
            </div>

            <article
              className={`manager-stage-card strategist ${selectedPhase.phase_type === "strategist" ? "selected" : ""}`}
              onClick={() => strategistPhase && setSelectedPhaseId(strategistPhase.id)}
            >
              <div className="manager-stage-head">
                <div>
                  <span className="manager-stage-kicker">2. Strategist Decision</span>
                  <h3>Planner Output</h3>
                </div>
                {payload.assets?.manager_sprite ? <img className="manager-stage-avatar" src={payload.assets.manager_sprite} alt="Strategist" /> : null}
              </div>
              <div className="manager-stage-list">
                <div className="manager-stage-list-item summary-item">
                  <span>Summary</span>
                  <strong>{strategistPhase?.decision_summary || "No strategist summary recorded."}</strong>
                </div>
                {strategistOutputs.map((item) => (
                  <div key={item.label} className="manager-stage-list-item">
                    <span>{item.label}</span>
                    <strong>{item.value}</strong>
                  </div>
                ))}
              </div>
            </article>

            <div className="manager-stage-arrow" aria-hidden="true">
              {"->"}
            </div>

            <article
              className={`manager-stage-card compiler ${selectedPhase.phase_type === "compiler" ? "selected" : ""}`}
              onClick={() => compilerPhase && setSelectedPhaseId(compilerPhase.id)}
            >
              <div className="manager-stage-head">
                <div>
                  <span className="manager-stage-kicker">3. Compiled Policy</span>
                  <h3>Policy Compiler</h3>
                </div>
                <div className="manager-system-badge">SYSTEM</div>
              </div>
              <div className="manager-stage-list">
                {compilerOutputs.map((item) => (
                  <div key={item.label} className="manager-stage-list-item">
                    <span>{item.label}</span>
                    <strong>{item.value}</strong>
                  </div>
                ))}
              </div>
            </article>

            <div className="manager-stage-arrow" aria-hidden="true">
              {"->"}
            </div>

            <article className="manager-stage-card factory">
              <div className="manager-stage-head">
                <div>
                  <span className="manager-stage-kicker">4. Factory Response</span>
                  <h3>Before / After</h3>
                </div>
              </div>
              <div className="manager-incident-chip-row">
                {incidentSummaries.length ? (
                  incidentSummaries.map((item) => (
                    <div key={item.label} className="manager-incident-chip">
                      <span>{item.label}</span>
                      <strong>{item.value}</strong>
                    </div>
                  ))
                ) : (
                  <div className="manager-empty-inline">No incidents recorded.</div>
                )}
              </div>
              <div className="manager-factory-region-grid">
                {factoryRegions.map((region) => (
                  <div key={region.region} className={`manager-factory-region-card ${region.highlighted ? "highlighted" : ""}`}>
                    <div className="manager-factory-region-head">
                      <strong>{region.region}</strong>
                      <span>{buildRegionSnapshotLabel(region)}</span>
                    </div>
                    <div className="manager-factory-region-metrics">
                      {region.metrics.map((metric) => (
                        <div key={metric.key} className="manager-factory-diff-row">
                          <span>{metric.metric}</span>
                          <strong>
                            {metric.before} {"->"} {metric.after} <em>{metric.delta >= 0 ? "+" : ""}{formatScalar(metric.delta)}</em>
                          </strong>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </article>

            <div className="manager-stage-arrow" aria-hidden="true">
              {"->"}
            </div>

            <article
              className={`manager-stage-card reviewer ${selectedPhase.phase_type === "reviewer" ? "selected" : ""}`}
              onClick={() => reviewerPhase && setSelectedPhaseId(reviewerPhase.id)}
            >
              <div className="manager-stage-head">
                <div>
                  <span className="manager-stage-kicker">5. Reviewer Assessment</span>
                  <h3>Outcome Diagnosis</h3>
                </div>
                {payload.assets?.manager_sprite ? <img className="manager-stage-avatar" src={payload.assets.manager_sprite} alt="Reviewer" /> : null}
              </div>
              <div className="manager-stage-list">
                <div className="manager-stage-list-item summary-item">
                  <span>Summary</span>
                  <strong>{reviewerPhase?.decision_summary || "No reviewer summary recorded."}</strong>
                </div>
                {reviewerOutputs.map((item) => (
                  <div key={item.label} className="manager-stage-list-item">
                    <span>{item.label}</span>
                    <strong>{item.value}</strong>
                  </div>
                ))}
              </div>
            </article>

            <div className="manager-stage-arrow" aria-hidden="true">
              {"->"}
            </div>

            <article className={`manager-stage-card feedback ${selectedPhase.phase_type === "reviewer" ? "linked" : ""}`}>
              <div className="manager-stage-head">
                <div>
                  <span className="manager-stage-kicker">6. Next-Day Carry Forward</span>
                  <h3>Seed for Tomorrow</h3>
                </div>
              </div>
              <div className="manager-stage-list feedback-list">
                {feedbackItems.map((item) => (
                  <div key={item.label} className="manager-stage-list-item">
                    <span>{item.label}</span>
                    <strong>{item.value}</strong>
                  </div>
                ))}
              </div>
            </article>
          </div>
        </section>

        <aside className="panel-card manager-detail-panel">
          <div className="manager-pane-head">
            <div>
              <div className="eyebrow">Selected Stage</div>
              <h2>Decision Detail</h2>
            </div>
            <div className={`manager-phase-pill ${phaseTone(selectedPhase.phase_type)}`}>{phaseLabel(selectedPhase.phase_type)}</div>
          </div>

          <section className="manager-detail-section">
            <h3>Inputs Received</h3>
            <div className="manager-token-list spacious">
              {selectedPhaseSignals.length ? (
                selectedPhaseSignals.map((signal) => (
                  <span key={`detail-${signal.key}`} className={`manager-token ${signal.tone}`} title={signal.detail}>
                    {signal.detail}
                  </span>
                ))
              ) : (
                <span className="muted">No stage inputs recorded.</span>
              )}
            </div>
          </section>

          <section className="manager-detail-section">
            <h3>Structured Output</h3>
            {structuredEntries.length ? (
              <div className="manager-accordion pipeline-accordion">
                {structuredEntries.map(([key, value]) => {
                  const open = expandedStructuredKeys.includes(key);
                  return (
                    <div key={key} className={`manager-accordion-item ${open ? "open" : ""}`}>
                      <button type="button" className="manager-accordion-button" onClick={() => toggleStructuredKey(key)}>
                        <span>{toPhrase(key)}</span>
                        <strong>{open ? "-" : "+"}</strong>
                      </button>
                      {open ? <div className="manager-accordion-body">{renderStructuredValue(value, key)}</div> : null}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="muted">No structured output recorded.</p>
            )}
          </section>

          <section className="manager-detail-section">
            <h3>Carry-Forward Risks</h3>
            {selectedPhaseCarry.length ? (
              <ul className="manager-list readable-list">
                {selectedPhaseCarry.map((entry, index) => (
                  <li key={`risk-${index}`}>{entry}</li>
                ))}
              </ul>
            ) : (
              <p className="muted">No carry-forward risks recorded for this stage.</p>
            )}
          </section>
        </aside>
      </section>
    </main>
  );
}

