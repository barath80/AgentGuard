import { useQuery } from "@tanstack/react-query";
import { Activity, BrainCircuit, Gauge, ReceiptText, Repeat, ShieldCheck, Timer, UserCog, Wallet, Zap, type LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { Badge, Card, EmptyState, ErrorState, PageHeader, Skeleton } from "../components/ui";
import { api, type AgentRisk, type Finding } from "../lib/api";
import { formatDateTime, timeAgo } from "../lib/format";

const KIND: Record<string, { icon: LucideIcon; title: string; tone: "critical" | "warning" | "neutral"; why: string }> = {
  denial_burst: {
    icon: Repeat,
    title: "Burst of policy denials",
    tone: "critical",
    why: "An agent repeatedly trying things it isn't allowed to is a common sign of prompt injection or a broken loop.",
  },
  "control:rate_limit": { icon: Gauge, title: "Call spike (rate limit hit)", tone: "warning", why: "Possible tool spam or a runaway loop." },
  "control:budget": { icon: Wallet, title: "Spend budget overrun", tone: "critical", why: "Calls were stopped because they would exceed the agent's 24h budget." },
  "control:circuit_open": { icon: Zap, title: "Circuit breaker opened", tone: "warning", why: "The agent was cut off after repeated denials." },
  "control:kill_switch": { icon: ShieldCheck, title: "Blocked by kill switch", tone: "neutral", why: "Calls arrived while the global kill switch was engaged." },
  refund_without_read: {
    icon: ReceiptText,
    title: "Refund without reading the ticket",
    tone: "warning",
    why: "An unusual sequence: money moved for a ticket the agent never successfully looked at.",
  },
  unresolved_timeout: {
    icon: Timer,
    title: "Unresolved timeout",
    tone: "critical",
    why: "The broker timed out and the target hasn't confirmed whether the side effect happened. Never assume it didn't.",
  },
};

function FindingRow({ finding }: { finding: Finding }) {
  const meta = KIND[finding.kind] ?? { icon: Activity, title: finding.kind, tone: "neutral" as const, why: "" };
  return (
    <li className="flex gap-4 py-4 first:pt-0 last:pb-0">
      <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface-2 text-ink-2">
        <meta.icon aria-hidden className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-medium text-ink">{meta.title}</p>
          <Badge tone={meta.tone}>{finding.agent_name}</Badge>
          <span className="text-xs text-ink-3">{finding.tenant_id}</span>
        </div>
        <p className="mt-0.5 text-[13px] text-ink-2">{finding.detail}</p>
        {meta.why && <p className="mt-0.5 text-xs text-ink-3">{meta.why}</p>}
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
          {finding.execution_ids.slice(0, 6).map((id) => (
            <Link key={id} to={`/activity/${id}`} className="font-mono text-xs text-accent-ink hover:underline">
              {id.slice(0, 8)}
            </Link>
          ))}
          {finding.execution_ids.length > 6 && <span className="text-xs text-ink-3">+{finding.execution_ids.length - 6} more</span>}
        </div>
      </div>
    </li>
  );
}

const FEATURE_LABEL: Record<string, string> = {
  calls: "call volume",
  deny_rate: "denial rate",
  approval_rate: "approval rate",
  policy_denials: "policy denials",
  control_denials: "runtime-control denials",
  tenant_mismatches: "cross-tenant attempts",
  schema_rejections: "unexpected arguments",
  environment_rejections: "unapproved environments",
  distinct_tools: "tools used",
  distinct_targets: "distinct targets",
  refunds: "refunds",
  refund_amount: "refund total",
  max_refund: "largest refund",
  refunds_without_read: "refunds without reading",
  failures: "failures",
  timeouts: "timeouts",
};

const RISK_TONE = { high: "critical", elevated: "warning", normal: "good" } as const;

function RiskRow({ row }: { row: AgentRisk }) {
  return (
    <li className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-medium text-ink">{row.agent_name}</p>
          <Badge tone={RISK_TONE[row.risk]}>{row.risk}</Badge>
          <span className="text-xs text-ink-3">
            {row.tenant_id} · window {timeAgo(row.window_start)} · {row.calls} calls
          </span>
        </div>
        <p className="mt-1 text-[13px] text-ink-2">
          {row.drivers.length
            ? row.drivers
                .map((d) => `${FEATURE_LABEL[d.feature] ?? d.feature} ${d.deviation > 0 ? "above" : "below"} usual (${Math.abs(d.deviation).toFixed(1)}×)`)
                .join(" · ")
            : "In line with learned behaviour."}
        </p>
      </div>
      <div className="tabular shrink-0 text-xs text-ink-3">p{Math.round(row.percentile * 100)}</div>
    </li>
  );
}

function RiskCard() {
  const risk = useQuery({ queryKey: ["risk"], queryFn: () => api.risk(24), refetchInterval: 60_000 });
  const data = risk.data;
  return (
    <Card
      className="mb-4"
      title={
        <span className="inline-flex items-center gap-1.5">
          <BrainCircuit aria-hidden className="size-4 text-accent" /> Behavioural risk
        </span>
      }
      subtitle={
        data?.model
          ? `Anomaly model trained ${timeAgo(data.model.trained_at)} on ${data.model.windows} windows. Advisory only — it never allows or denies.`
          : "Learned from recorded traffic. Advisory only — it never allows or denies."
      }
    >
      {risk.error && <ErrorState error={risk.error} />}
      {!data ? (
        <Skeleton className="h-24" />
      ) : !data.available ? (
        <p className="text-[13px] text-ink-3">
          No model yet: {data.reason}. Train one with <code className="font-mono">python -m agentguard.ml.model train</code>.
        </p>
      ) : data.agents.length ? (
        <ul className="divide-y divide-line">
          {data.agents.map((row) => (
            <RiskRow key={row.agent_id} row={row} />
          ))}
        </ul>
      ) : (
        <p className="text-[13px] text-ink-3">No agent activity in the last 24 hours.</p>
      )}
    </Card>
  );
}

export function Insights() {
  const findings = useQuery({ queryKey: ["anomalies"], queryFn: api.anomalies, refetchInterval: 60_000 });
  const actions = useQuery({ queryKey: ["operator-actions"], queryFn: api.operatorActions, refetchInterval: 60_000 });

  return (
    <>
      <PageHeader
        title="Insights"
        description="Patterns worth a human look — including ones that were already blocked — and a log of every action taken in this console."
      />
      <RiskCard />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <Card title="Anomaly findings" subtitle="Derived read-only from the execution trace">
          {findings.error && <ErrorState error={findings.error} />}
          {!findings.data ? (
            <Skeleton className="h-40" />
          ) : findings.data.length ? (
            <ul className="divide-y divide-line">
              {findings.data.map((f, i) => (
                <FindingRow key={`${f.kind}-${f.agent_id}-${i}`} finding={f} />
              ))}
            </ul>
          ) : (
            <EmptyState icon={ShieldCheck} title="No anomalies detected">
              Denial bursts, spikes, budget overruns, unusual sequences and unresolved timeouts show up here.
            </EmptyState>
          )}
        </Card>

        <Card title="Operator log" subtitle="Append-only record of console actions">
          {actions.error && <ErrorState error={actions.error} />}
          {!actions.data ? (
            <Skeleton className="h-40" />
          ) : actions.data.length ? (
            <ol className="space-y-3">
              {actions.data.map((a) => (
                <li key={a.id} className="flex gap-3 text-[13px]">
                  <UserCog aria-hidden className="mt-0.5 size-4 shrink-0 text-ink-3" />
                  <div className="min-w-0">
                    <p className="text-ink">
                      <span className="font-medium">{a.actor}</span> · <span className="font-mono text-[12px]">{a.action}</span>
                    </p>
                    <p className="truncate text-xs text-ink-3">
                      {a.target_type} {a.target_id.slice(0, 8)} · {formatDateTime(a.created_at)}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-[13px] text-ink-3">No console actions yet.</p>
          )}
        </Card>
      </div>
    </>
  );
}
