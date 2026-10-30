import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import {
  BadgeCheck,
  Ban,
  CircleDashed,
  CircleDot,
  Cog,
  FileClock,
  Fingerprint,
  Gavel,
  ScrollText,
  ShieldAlert,
  UserCheck,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef } from "react";
import type { ExecutionDetail } from "../lib/api";
import { api } from "../lib/api";
import { describeRule, eventLabel, formatDateTime, formatMs } from "../lib/format";
import { DecisionBadge, ErrorState, Hash, KeyValue, Mono, OutcomeBadge, Skeleton } from "./ui";

type StepState = "passed" | "stopped" | "waiting" | "skipped" | "warning";

interface Step {
  icon: LucideIcon;
  title: string;
  state: StepState;
  detail: React.ReactNode;
}

const STATE_STYLE: Record<StepState, { ring: string; label: string; Icon: LucideIcon }> = {
  passed: { ring: "bg-good-soft text-good-ink ring-good/30", label: "Passed", Icon: BadgeCheck },
  stopped: { ring: "bg-critical-soft text-critical-ink ring-critical/30", label: "Stopped here", Icon: Ban },
  waiting: { ring: "bg-warning-soft text-warning-ink ring-warning/40", label: "Waiting", Icon: CircleDot },
  warning: { ring: "bg-warning-soft text-warning-ink ring-warning/40", label: "Needs attention", Icon: ShieldAlert },
  skipped: { ring: "bg-surface-2 text-ink-3 ring-line", label: "Not reached", Icon: CircleDashed },
};

/** Reconstruct the gateway pipeline for one call, from its recorded decision and audit trail. */
function pipeline(e: ExecutionDetail): Step[] {
  const prePolicyDeny = e.decision === "deny" && e.policy_version === -1;
  const policyDeny = e.decision === "deny" && !prePolicyDeny;
  const approval = e.approval;
  const reachedBroker = e.executed_at !== null || ["success", "failure", "timeout"].includes(e.outcome);

  const steps: Step[] = [
    {
      icon: Fingerprint,
      title: "Identity resolved",
      state: "passed",
      detail: (
        <>
          {e.agent_name} · {e.tenant_id}, derived from the verified workload identity — not from the request body.
        </>
      ),
    },
    {
      icon: ShieldAlert,
      title: "Pre-policy checks",
      state: prePolicyDeny ? "stopped" : "passed",
      detail: prePolicyDeny
        ? e.reason
        : "Schema, environment, tenant boundary, kill switch, circuit breaker, rate limit and budget all passed.",
    },
    {
      icon: Gavel,
      title: e.policy_version > 0 ? `Policy v${e.policy_version}` : "Policy",
      state: prePolicyDeny ? "skipped" : policyDeny ? "stopped" : "passed",
      detail: prePolicyDeny ? (
        "Not evaluated — the call was stopped earlier."
      ) : e.matched_rule ? (
        <>
          Matched rule (priority {e.matched_rule.priority}): {describeRule(e.matched_rule)}
        </>
      ) : e.decision === "deny" ? (
        "No rule matched — implicit deny (fail closed)."
      ) : (
        e.reason ?? "Decision recorded."
      ),
    },
  ];

  if (e.decision === "approval_required" || approval) {
    steps.push({
      icon: UserCheck,
      title: "Human approval",
      state:
        approval?.status === "consumed" || approval?.status === "approved"
          ? "passed"
          : approval?.status === "pending"
            ? "waiting"
            : "stopped",
      detail: approval ? (
        <>
          {approval.status === "pending" && `Waiting for ${approval.required_role ?? "an approver"} in ${approval.tenant_id}.`}
          {approval.status === "approved" && `Approved by ${approval.approver}; not yet executed.`}
          {approval.status === "consumed" && `Approved by ${approval.approver}, consumed once at execution.`}
          {approval.status === "denied" && `Rejected by ${approval.approver}.`}
          {approval.status === "expired" && "Expired before anyone decided."}
        </>
      ) : (
        "Approval record missing."
      ),
    });
  }

  steps.push({
    icon: Cog,
    title: "Broker execution",
    state: !reachedBroker
      ? "skipped"
      : e.outcome === "success"
        ? "passed"
        : e.outcome === "timeout"
          ? "warning"
          : "stopped",
    detail: !reachedBroker
      ? "Never executed — no credential was used."
      : e.outcome === "success"
        ? `Executed with a single-scope credential${e.reason ? ` (${e.reason})` : ""}.`
        : e.reason,
  });
  steps.push({
    icon: ScrollText,
    title: "Audit trail",
    state: e.trace_complete ? "passed" : e.outcome === "not_attempted" ? "waiting" : "warning",
    detail: `${e.audit_events.length} append-only event${e.audit_events.length === 1 ? "" : "s"}; trace ${
      e.trace_complete ? "complete" : "incomplete"
    }.`,
  });
  return steps;
}

function Pipeline({ execution }: { execution: ExecutionDetail }) {
  return (
    <ol className="relative space-y-0">
      {pipeline(execution).map((step, i, all) => {
        const style = STATE_STYLE[step.state];
        return (
          <li key={step.title} className="relative flex gap-3 pb-4 last:pb-0">
            {i < all.length - 1 && <span aria-hidden className="absolute left-[15px] top-8 bottom-0 w-px bg-line" />}
            <div className={clsx("relative z-[1] grid size-8 shrink-0 place-items-center rounded-full ring-1", style.ring)}>
              <step.icon aria-hidden className="size-4" />
            </div>
            <div className="min-w-0 flex-1 pt-1">
              <div className="flex flex-wrap items-center gap-2">
                <p className="font-medium text-ink">{step.title}</p>
                <span className={clsx("inline-flex items-center gap-1 text-[11px] font-medium", style.ring.split(" ")[1])}>
                  <style.Icon aria-hidden className="size-3" />
                  {style.label}
                </span>
              </div>
              <p className="mt-0.5 text-[13px] text-ink-2">{step.detail}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export function ExecutionDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const execution = useQuery({
    queryKey: ["execution", id],
    queryFn: () => api.execution(id),
    refetchInterval: (q) => (q.state.data?.trace_complete ? false : 5_000),
  });
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => ev.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    panel.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const e = execution.data;

  return (
    <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" aria-labelledby="execution-title">
      <div className="absolute inset-0 bg-black/30 backdrop-blur-[1px]" onClick={onClose} />
      <div
        ref={panel}
        tabIndex={-1}
        className="absolute inset-y-0 right-0 flex w-full max-w-[640px] flex-col border-l border-line bg-page shadow-pop outline-none"
      >
        <header className="flex items-start justify-between gap-4 border-b border-line bg-surface px-6 py-4">
          <div className="min-w-0">
            <p className="text-xs text-ink-3">Tool call</p>
            <h2 id="execution-title" className="mt-0.5 truncate font-mono text-[15px] font-semibold text-ink">
              {e ? `${e.tool_name} → ${e.target_resource}` : "Loading…"}
            </h2>
            {e && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <DecisionBadge decision={e.decision} />
                <OutcomeBadge outcome={e.outcome} />
                <span className="text-xs text-ink-3">{formatDateTime(e.requested_at)}</span>
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="grid size-8 shrink-0 place-items-center rounded-md text-ink-3 hover:bg-surface-hover hover:text-ink"
          >
            <X aria-hidden className="size-4" />
          </button>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto px-6 py-5">
          {execution.error && <ErrorState error={execution.error} />}
          {!e && !execution.error && (
            <div className="space-y-3">
              <Skeleton className="h-40" />
              <Skeleton className="h-32" />
            </div>
          )}
          {e && (
            <>
              <section>
                <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-3">Decision path</h3>
                <Pipeline execution={e} />
              </section>

              <section className="rounded-xl border border-line bg-surface p-4">
                <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-3">Request</h3>
                <KeyValue
                  items={[
                    ["Execution", <Hash key="id" value={e.id} length={36} />],
                    ["Agent", `${e.agent_name} (${e.tenant_id})`],
                    ["Environment", <Mono key="env">{e.environment}</Mono>],
                    ["Idempotency key", <Hash key="idem" value={e.idempotency_key} length={24} />],
                    ["Workflow", e.workflow_id ? <Mono key="wf">{e.workflow_id}</Mono> : "—"],
                    ["Request ref", <Hash key="req" value={e.safe_request_ref} length={24} />],
                    ["Result ref", <Hash key="res" value={e.safe_result_ref} length={24} />],
                    ["Decision latency", formatMs(e.latency_ms)],
                  ]}
                />
                <div className="mt-4">
                  <p className="mb-1.5 text-xs text-ink-3">Arguments</p>
                  {e.payload_visible ? (
                    <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-[12px] text-ink">
                      {JSON.stringify(e.args ?? {}, null, 2)}
                    </pre>
                  ) : (
                    <p className="rounded-lg bg-surface-2 p-3 text-[13px] text-ink-3">
                      Hidden for your role. The request ref above is a hash of the arguments.
                    </p>
                  )}
                </div>
              </section>

              {e.approval && (
                <section className="rounded-xl border border-line bg-surface p-4">
                  <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-3">Approval binding</h3>
                  <KeyValue
                    items={[
                      ["Status", e.approval.status],
                      ["Bound action hash", <Hash key="ah" value={e.approval.action_hash} length={24} />],
                      ["Bound resource", <Mono key="r">{e.approval.resource}</Mono>],
                      ["Bound policy", `v${e.approval.policy_version}`],
                      ["Required role", e.approval.required_role ?? "any"],
                      ["Approver", e.approval.approver ?? "—"],
                      ["Expires", formatDateTime(e.approval.expires_at)],
                      ["Consumed", formatDateTime(e.approval.consumed_at)],
                    ]}
                  />
                </section>
              )}

              <section>
                <h3 className="mb-3 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-ink-3">
                  <FileClock aria-hidden className="size-3.5" /> Audit events
                </h3>
                <ol className="space-y-2">
                  {e.audit_events.map((ev) => (
                    <li key={ev.id} className="rounded-lg border border-line bg-surface px-3.5 py-2.5">
                      <div className="flex items-baseline justify-between gap-3">
                        <p className="text-[13px] font-medium text-ink">{eventLabel(ev.event_type)}</p>
                        <p className="tabular shrink-0 text-xs text-ink-3">{formatDateTime(ev.created_at)}</p>
                      </div>
                      {Object.keys(ev.payload ?? {}).length > 0 && (
                        <p className="mt-1 break-all font-mono text-[11.5px] text-ink-3">
                          {Object.entries(ev.payload)
                            .filter(([, v]) => v !== null)
                            .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
                            .join("  ")}
                        </p>
                      )}
                    </li>
                  ))}
                </ol>
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
