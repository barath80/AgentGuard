// Typed client for the gateway's /v1/console API. Same-origin only: nginx (prod)
// or the Vite dev proxy forwards /v1 to the gateway, so no CORS is ever opened.

export type Decision = "allow" | "deny" | "approval_required";

export interface Me {
  name: string;
  roles: string[];
  tenants: string[];
  can_operate: boolean;
  can_view_payloads: boolean;
  reads_all_tenants: boolean;
}

export interface ExecutionSummary {
  id: string;
  agent_id: string;
  agent_name: string;
  tenant_id: string;
  tool_name: string;
  environment: string;
  target_resource: string;
  policy_version: number;
  decision: Decision;
  outcome: string;
  reason: string | null;
  approval_id: string | null;
  workflow_id: string | null;
  requested_at: string | null;
  completed_at: string | null;
  latency_ms: number | null;
  trace_complete: boolean;
}

export interface RuleMatch {
  tool?: string | string[];
  risk_class?: string | string[];
  environment?: string | string[];
  tenant?: string | string[];
  agent_risk_category?: string | string[];
  arg?: { field: string; op: string; value: unknown };
}

export interface Rule {
  id?: string;
  priority: number;
  decision: Decision;
  approval_role?: string | null;
  match: RuleMatch;
}

export interface Approval {
  id: string;
  execution_id: string;
  tenant_id: string;
  resource: string;
  action_hash: string;
  policy_version: number;
  required_role: string | null;
  status: "pending" | "approved" | "denied" | "consumed" | "expired";
  approver: string | null;
  created_at: string | null;
  expires_at: string | null;
  decided_at: string | null;
  consumed_at: string | null;
  can_decide: boolean;
  blocked_reason: string | null;
  tool_name?: string | null;
  environment?: string | null;
  agent_name?: string | null;
  args?: Record<string, unknown> | null;
}

export interface AuditEvent {
  id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string | null;
}

export interface ExecutionDetail extends ExecutionSummary {
  idempotency_key: string;
  decided_at: string | null;
  executed_at: string | null;
  safe_request_ref: string | null;
  safe_result_ref: string | null;
  args: Record<string, unknown> | null;
  payload_visible: boolean;
  matched_rule: Rule | null;
  approval: Approval | null;
  audit_events: AuditEvent[];
}

export interface SeriesBucket {
  start: string;
  allow: number;
  deny: number;
  approval_required: number;
}

export interface Overview {
  window_hours: number;
  generated_at: string;
  totals: {
    calls: number;
    by_decision: Partial<Record<Decision, number>>;
    by_outcome: Record<string, number>;
    by_tool: Record<string, number>;
  };
  latency_ms: { p50: number | null; p95: number | null };
  trace_completeness: number | null;
  unresolved_timeouts: number;
  pending_approvals: number;
  denials_by_reason: Record<string, number>;
  series: SeriesBucket[];
  recent: ExecutionSummary[];
  posture: {
    environment: string;
    kill_switch: boolean;
    allow_reads_when_policy_unavailable: boolean;
    active_policy_version: number | null;
    breaker_threshold: number;
    breaker_window_seconds: number;
    approval_expiry_seconds: number;
  };
}

export interface Agent {
  id: string;
  name: string;
  owner: string;
  tenant_id: string;
  workload_identity_ref: string;
  status: string;
  approved_environments: string[];
  risk_category: string;
  rate_limit_per_minute: number;
  daily_spend_limit: number | null;
  spent_24h: number;
  calls_24h: number;
  denials_24h: number;
  circuit_open: boolean;
  last_seen_at: string | null;
  created_at: string | null;
}

export interface Tool {
  id: string;
  name: string;
  schema_version: string;
  input_schema: { properties?: Record<string, { type?: string }> };
  risk_class: string;
  target_scope: string;
  executor_ref: string;
  spend_arg: string | null;
  calls_total: number;
}

export interface PolicyVersion {
  id: string;
  version: number;
  scope: string;
  is_active: boolean;
  published_at: string | null;
  rules: Rule[];
}

export interface ProposedPolicy {
  name: string;
  description: string | null;
  policy: { version: number; description?: string; rules: Rule[] };
}

export interface DecisionChange {
  execution_id: string;
  tool_name: string;
  recorded: Decision;
  simulated: Decision;
  impact: "security_loosening" | "task_success_risk" | "other";
  agent_name: string | null;
  environment: string | null;
  target_resource: string | null;
  requested_at: string | null;
}

export interface SimulationReport {
  proposed_version: number;
  evaluated: number;
  skipped_pre_policy: number;
  transitions: Record<string, number>;
  newly_allowed: number;
  newly_restricted: number;
  unattended_rate_before: number;
  unattended_rate_after: number;
  verdict: "loosens_security" | "safe_to_review";
  changes: DecisionChange[];
}

export interface ReplayReport {
  replayed: number;
  reproduced: number;
  skipped_pre_policy: number;
  by_policy_version: Record<string, { replayed: number; reproduced: number }>;
  mismatches: { execution_id: string; tool_name: string; recorded: string; replayed: string | null }[];
}

export interface Finding {
  kind: string;
  agent_id: string;
  agent_name: string;
  tenant_id: string;
  detail: string;
  execution_ids: string[];
}

export interface OperatorAction {
  id: string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  detail: Record<string, unknown>;
  created_at: string | null;
}

export interface AgentRisk {
  agent_id: string;
  agent_name: string;
  tenant_id: string;
  window_start: string;
  score: number;
  percentile: number;
  max_deviation: number;
  risk: "normal" | "elevated" | "high";
  calls: number;
  drivers: { feature: string; deviation: number }[];
  execution_ids: string[];
}

export interface RiskReport {
  available: boolean;
  reason?: string;
  model?: { trained_at: string; windows: number; agents: number };
  agents: AgentRisk[];
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

const TOKEN_KEY = "agentguard.console.token";

export const tokenStore = {
  get(): string | null {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(token: string | null) {
    try {
      if (token) sessionStorage.setItem(TOKEN_KEY, token);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* storage unavailable: the session lasts only as long as this page */
    }
  },
};

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler;
}

async function request<T>(path: string, init: RequestInit = {}, token = tokenStore.get()): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    if (response.status === 401) onUnauthorized();
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export interface AgentSpec {
  workload_identity_ref: string;
  name?: string;
  owner: string;
  tenant_id: string;
  approved_environments: ("dev" | "staging" | "prod")[];
  risk_category: "low" | "medium" | "high";
  rate_limit_per_minute: number;
  daily_spend_limit: number | null;
}

export interface PublishResult {
  id: string;
  version: number;
  previous_version: number;
  newly_allowed: number;
  newly_restricted: number;
}

export type WorkflowStatus = "partial" | "in_progress" | "incomplete" | "completed";

export interface WorkflowStep {
  execution_id: string;
  tool_name: string;
  target_resource: string;
  decision: Decision;
  outcome: string;
  reason: string | null;
  state: "done" | "waiting" | "blocked" | "failed" | "unconfirmed";
  writes: boolean;
  requested_at: string | null;
}

export interface Workflow {
  workflow_id: string;
  agent_id: string;
  agent_name: string;
  tenant_id: string;
  status: WorkflowStatus;
  started_at: string | null;
  last_step_at: string | null;
  steps: WorkflowStep[];
  side_effects: WorkflowStep[];
}

export interface ExecutionFilters {
  decision?: string;
  outcome?: string;
  tool?: string;
  agent_id?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export const api = {
  me: (token?: string) => request<Me>("/v1/console/me", {}, token ?? tokenStore.get()),
  overview: (windowHours: number) => request<Overview>(`/v1/console/overview${qs({ window_hours: windowHours })}`),
  executions: (filters: ExecutionFilters) =>
    request<{ total: number; items: ExecutionSummary[] }>(`/v1/console/executions${qs({ ...filters })}`),
  execution: (id: string) => request<ExecutionDetail>(`/v1/console/executions/${encodeURIComponent(id)}`),
  approvals: (status?: string) => request<Approval[]>(`/v1/console/approvals${qs({ status })}`),
  decide: (id: string, approve: boolean) =>
    request<Approval>(`/v1/console/approvals/${encodeURIComponent(id)}/decide`, {
      method: "POST",
      body: JSON.stringify({ approve }),
    }),
  agents: () => request<Agent[]>("/v1/console/agents"),
  setSuspended: (id: string, suspended: boolean) =>
    request<{ id: string; status: string }>(
      `/v1/console/agents/${encodeURIComponent(id)}/${suspended ? "suspend" : "resume"}`,
      { method: "POST" },
    ),
  registerAgent: (spec: AgentSpec) =>
    request<{ id: string; name: string }>("/v1/console/agents", { method: "POST", body: JSON.stringify(spec) }),
  tools: () => request<Tool[]>("/v1/console/tools"),
  policies: () => request<{ versions: PolicyVersion[]; proposed: ProposedPolicy[] }>("/v1/console/policies"),
  simulate: (policy: unknown) =>
    request<SimulationReport>("/v1/console/simulations", { method: "POST", body: JSON.stringify(policy) }),
  publishPolicy: (body: { description?: string; rules: Rule[]; acknowledge_loosening: boolean; reason?: string }) =>
    request<PublishResult>("/v1/console/policies", { method: "POST", body: JSON.stringify(body) }),
  replay: () => request<ReplayReport>("/v1/console/replay", { method: "POST" }),
  workflows: (status?: WorkflowStatus) => request<Workflow[]>(`/v1/console/workflows${qs({ status })}`),
  anomalies: () => request<Finding[]>("/v1/console/anomalies"),
  operatorActions: () => request<OperatorAction[]>("/v1/console/operator-actions"),
  risk: (hours = 24) => request<RiskReport>(`/v1/console/risk${qs({ hours })}`),
};
