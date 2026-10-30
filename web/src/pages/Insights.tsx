import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { useState } from "react";
import { Activity, BrainCircuit, ChevronDown, Clock, FlaskConical, Gauge, ReceiptText, Repeat, ShieldCheck, Timer, UserCog, Wallet, Zap, type LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, EmptyState, ErrorState, PageHeader, Skeleton } from "../components/ui";
import { WorkflowsCard } from "../components/WorkflowsCard";
import { api, type AgentRisk, type Finding, type RiskReport } from "../lib/api";
import { formatDateTime, formatPercent, timeAgo } from "../lib/format";

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
  clock_skew: {
    icon: Clock,
    title: "Clock skew between services",
    tone: "warning",
    why: "The broker's clock disagrees with the gateway's, so the order of events across services in this trace can't be trusted. Check NTP on both.",
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
            {row.tenant_id} · worst window {timeAgo(row.window_start)} · {row.calls} calls
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
      <div className="shrink-0 text-right text-xs text-ink-3 sm:w-36">
        <p className="tabular text-ink-2">
          {row.flagged_windows} of {row.windows} windows flagged
        </p>
        <div
          className="mt-1 h-1.5 rounded-full bg-surface-2"
          role="meter"
          aria-label="Share of this agent's windows flagged"
          aria-valuenow={row.windows ? (row.flagged_windows / row.windows) * 100 : 0}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className={clsx("h-full rounded-full", row.risk === "high" ? "bg-critical" : row.risk === "elevated" ? "bg-warning" : "bg-good")}
            style={{ width: `${row.windows ? Math.max(4, (row.flagged_windows / row.windows) * 100) : 0}%` }}
          />
        </div>
      </div>
    </li>
  );
}

const DETECTOR_LABEL: Record<string, string> = {
  isolation_forest: "Isolation Forest",
  local_outlier_factor: "Local Outlier Factor",
  one_class_svm: "One-Class SVM",
  autoencoder: "Neural autoencoder",
  gradient_boosting: "Gradient boosting",
  random_forest: "Random forest",
  neural_classifier: "Neural classifier",
  ensemble: "Ensemble",
};

const DETECTOR_HINT: Record<string, string> = {
  ensemble: "Boosting + autoencoder · ",
};

function ModelStat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-line bg-surface-2 px-3 py-2.5">
      <p className="truncate text-xs text-ink-3">{label}</p>
      <p className="tabular mt-0.5 truncate text-lg font-semibold text-ink">{value}</p>
      {hint && <p className="line-clamp-2 text-[11px] leading-snug text-ink-3">{hint}</p>}
    </div>
  );
}

/** What model is serving risk scores, and how it did on traffic it never trained on. */
function ModelSummary({ model }: { model: NonNullable<RiskReport["model"]> }) {
  const e = model.evaluation;
  return (
    <div className="mb-5">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
        <ModelStat
          label="Detector"
          value={DETECTOR_LABEL[model.detector] ?? model.detector}
          hint={`${DETECTOR_HINT[model.detector] ?? ""}${model.window_minutes} min windows · ${model.windows} trained on`}
        />
        {e ? (
          <>
            <ModelStat label="PR-AUC" value={e.test_pr_auc?.toFixed(2) ?? "—"} hint="ranking attacks above normal" />
            <ModelStat label="Precision" value={formatPercent(e.test_precision, 0)} hint="flags that were real attacks" />
            <ModelStat label="Recall" value={formatPercent(e.test_recall, 0)} hint="attack windows caught" />
            <ModelStat label="False alarms" value={formatPercent(e.test_false_positive_rate, 1)} hint="of normal windows flagged" />
            <ModelStat
              label="New attack types"
              value={e.test_unseen_recall === undefined || e.test_unseen_recall === null ? "—" : formatPercent(e.test_unseen_recall, 0)}
              hint="caught without training on them"
            />
          </>
        ) : (
          <div className="col-span-1 flex items-center rounded-lg border border-dashed border-line-strong px-3 py-2.5 text-xs text-ink-3 sm:col-span-2 xl:col-span-5">
            Trained without labels, so there's no held-out evaluation. The notebook (notebooks/anomaly_model.ipynb) trains and scores one on labelled traffic.
          </div>
        )}
      </div>
      {e && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-ink-3">
          <FlaskConical aria-hidden className="size-3.5" />
          Measured on {e.test_windows ?? "held-out"} windows ({e.test_attack_windows ?? "?"} with attacks) from after the training
          period, through the same scoring the console uses.
        </p>
      )}
    </div>
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
          ? `Trained ${timeAgo(data.model.trained_at)}. Advisory only: it never allows or denies a call.`
          : "Learned from recorded traffic. Advisory only — it never allows or denies."
      }
    >
      {risk.error && <ErrorState error={risk.error} />}
      {!data ? (
        <Skeleton className="h-24" />
      ) : !data.available ? (
        <p className="text-[13px] text-ink-3">
          No model yet: {data.reason}. Train one in <code className="font-mono">notebooks/anomaly_model.ipynb</code>, or run{" "}
          <code className="font-mono">python -m agentguard.ml.model train</code>.
        </p>
      ) : (
        <>
          {data.model && <ModelSummary model={data.model} />}
          {data.agents.length ? (
            <ul className="divide-y divide-line">
              {data.agents.map((row) => (
                <RiskRow key={row.agent_id} row={row} />
              ))}
            </ul>
          ) : (
            <p className="text-[13px] text-ink-3">No agent activity in the last 24 hours.</p>
          )}
        </>
      )}
    </Card>
  );
}

const FINDINGS_SHOWN = 10;

export function Insights() {
  const findings = useQuery({ queryKey: ["anomalies"], queryFn: api.anomalies, refetchInterval: 60_000 });
  const [allFindings, setAllFindings] = useState(false);
  const actions = useQuery({ queryKey: ["operator-actions"], queryFn: api.operatorActions, refetchInterval: 60_000 });

  return (
    <>
      <PageHeader
        title="Insights"
        description="Patterns worth a human look — including ones that were already blocked — and a log of every action taken in this console."
      />
      <RiskCard />
      <WorkflowsCard />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <Card
          title="Anomaly findings"
          subtitle={findings.data ? `${findings.data.length} in the last 7 days, derived read-only from the execution trace` : "Derived read-only from the execution trace"}
        >
          {findings.error && <ErrorState error={findings.error} />}
          {!findings.data ? (
            <Skeleton className="h-40" />
          ) : findings.data.length ? (
            <>
              <ul className="divide-y divide-line">
                {(allFindings ? findings.data : findings.data.slice(0, FINDINGS_SHOWN)).map((f, i) => (
                  <FindingRow key={`${f.kind}-${f.agent_id}-${i}`} finding={f} />
                ))}
              </ul>
              {findings.data.length > FINDINGS_SHOWN && (
                <Button variant="ghost" icon={ChevronDown} className="mt-3 w-full" onClick={() => setAllFindings((v) => !v)}>
                  {allFindings ? "Show fewer" : `Show all ${findings.data.length}`}
                </Button>
              )}
            </>
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
