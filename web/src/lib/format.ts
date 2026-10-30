import type { Decision, Rule, RuleMatch } from "./api";

export const DECISION_LABEL: Record<Decision, string> = {
  allow: "Allowed",
  deny: "Denied",
  approval_required: "Needs approval",
};

const OPS: Record<string, string> = { gt: ">", gte: "≥", lt: "<", lte: "≤", eq: "=" };

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value < 10 ? `${value.toFixed(1)} ms` : `${Math.round(value)} ms`;
}

export function timeAgo(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  if (seconds < 0) return timeUntil(iso, now);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function timeUntil(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  if (seconds <= 0) return "expired";
  if (seconds < 60) return `${seconds}s left`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s left`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m left`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function shortId(id: string | null | undefined, length = 8): string {
  if (!id) return "—";
  return id.replace(/^sha256:/, "").slice(0, length);
}

function list(value: string | string[]): string {
  return Array.isArray(value) ? value.join(" or ") : value;
}

/** Plain-English conditions for a rule's match clause, e.g. ["tool is issue-refund", "amount > 500"]. */
export function describeMatch(match: RuleMatch): string[] {
  const parts: string[] = [];
  const add = (label: string, value: string | string[] | undefined) => {
    if (value !== undefined && value !== "*") parts.push(`${label} is ${list(value)}`);
  };
  add("tool", match.tool);
  add("risk class", match.risk_class);
  add("environment", match.environment);
  add("tenant", match.tenant);
  add("agent risk", match.agent_risk_category);
  if (match.arg) parts.push(`${match.arg.field} ${OPS[match.arg.op] ?? match.arg.op} ${String(match.arg.value)}`);
  return parts;
}

export function describeRule(rule: Rule): string {
  const when = describeMatch(rule.match);
  const then =
    rule.decision === "approval_required"
      ? `require approval${rule.approval_role ? ` from ${rule.approval_role}` : ""}`
      : rule.decision;
  return `${when.length ? `When ${when.join(" and ")}` : "Always"} → ${then}`;
}

const EVENT_LABEL: Record<string, string> = {
  decision_made: "Policy decision recorded",
  approval_requested: "Approval requested",
  approval_decided: "Approval decided",
  approval_consumed: "Approval consumed (single use)",
  execution_completed: "Executed by broker",
  execution_failed: "Execution failed",
  execution_timeout: "Broker timed out",
  reconciliation: "Reconciled with target system",
};

export function eventLabel(type: string): string {
  return EVENT_LABEL[type] ?? type.replace(/_/g, " ");
}

/** Group the server's reason strings into short labels for charts. */
export function reasonLabel(reason: string): string {
  if (reason.startsWith("control:")) {
    const kind = reason.slice("control:".length);
    return `Control: ${kind.replace(/_/g, " ")}`;
  }
  return reason.charAt(0).toUpperCase() + reason.slice(1);
}
