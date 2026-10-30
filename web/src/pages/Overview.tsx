import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Inbox, Radar, Timer, Waypoints } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { BarList, DecisionSplit, DecisionTimeline } from "../components/charts";
import { ExecutionTable } from "../components/ExecutionTable";
import { Card, EmptyState, ErrorState, PageHeader, Segmented, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { formatMs, formatNumber, formatPercent, reasonLabel } from "../lib/format";
import { useMe } from "../lib/auth";

const WINDOWS = [
  { value: 1, label: "1h" },
  { value: 24, label: "24h" },
  { value: 168, label: "7d" },
];

function Stat({
  label,
  value,
  hint,
  tone,
  to,
}: {
  label: string;
  value: string;
  hint?: React.ReactNode;
  tone?: "warning" | "critical";
  to?: string;
}) {
  const body = (
    <div className="h-full rounded-xl border border-line bg-surface p-4 shadow-card transition hover:border-line-strong">
      <p className="text-xs font-medium text-ink-3">{label}</p>
      <p
        className={
          "mt-1.5 text-[26px] font-semibold leading-none tracking-tight " +
          (tone === "critical" ? "text-critical-ink" : tone === "warning" ? "text-warning-ink" : "text-ink")
        }
      >
        {value}
      </p>
      {hint && <div className="mt-2 text-xs text-ink-3">{hint}</div>}
    </div>
  );
  return to ? (
    <Link to={to} className="block rounded-xl">
      {body}
    </Link>
  ) : (
    body
  );
}

export function Overview() {
  const me = useMe();
  const navigate = useNavigate();
  const [windowHours, setWindowHours] = useState(24);
  const overview = useQuery({
    queryKey: ["overview", windowHours],
    queryFn: () => api.overview(windowHours),
    refetchInterval: 60_000,
  });
  const anomalies = useQuery({ queryKey: ["anomalies"], queryFn: api.anomalies, refetchInterval: 60_000 });
  const data = overview.data;
  const by = data?.totals.by_decision ?? {};
  const calls = data?.totals.calls ?? 0;
  const rate = (n: number | undefined) => (calls ? (n ?? 0) / calls : null);

  return (
    <>
      <PageHeader
        title="Overview"
        description={
          <>
            Every tool call that crossed the gateway{me.reads_all_tenants ? " across all tenants" : ` for ${me.tenants.join(", ")}`}.
            Updates live.
          </>
        }
        actions={<Segmented label="Time window" value={windowHours} options={WINDOWS} onChange={setWindowHours} />}
      />

      {overview.error && <ErrorState error={overview.error} />}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {data ? (
          <>
            <Stat
              label="Tool calls"
              value={formatNumber(calls)}
              hint={
                <div className="space-y-1.5">
                  <DecisionSplit counts={by} />
                </div>
              }
            />
            <Stat label="Allowed" value={formatPercent(rate(by.allow), 0)} hint={`${formatNumber(by.allow ?? 0)} ran unattended`} />
            <Stat
              label="Denied"
              value={formatPercent(rate(by.deny), 0)}
              hint={`${formatNumber(by.deny ?? 0)} blocked before execution`}
            />
            <Stat
              label="Awaiting approval"
              value={formatNumber(data.pending_approvals)}
              tone={data.pending_approvals ? "warning" : undefined}
              hint={
                <span className="inline-flex items-center gap-1 text-accent-ink">
                  Open queue <ArrowRight aria-hidden className="size-3" />
                </span>
              }
              to="/approvals"
            />
            <Stat
              label="Decision latency p95"
              value={formatMs(data.latency_ms.p95)}
              hint={`p50 ${formatMs(data.latency_ms.p50)}`}
            />
            <Stat
              label="Trace completeness"
              value={formatPercent(data.trace_completeness)}
              tone={data.unresolved_timeouts ? "warning" : undefined}
              hint={
                data.unresolved_timeouts
                  ? `${data.unresolved_timeouts} unresolved timeout${data.unresolved_timeouts > 1 ? "s" : ""}`
                  : "No unresolved timeouts"
              }
            />
          </>
        ) : (
          Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-[108px] rounded-xl" />)
        )}
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[1fr_360px]">
        <Card title="Decisions over time" subtitle="Stacked by policy outcome">
          {data ? (
            calls ? (
              <DecisionTimeline series={data.series} windowHours={windowHours} />
            ) : (
              <EmptyState icon={Waypoints} title="No tool calls in this window">
                Run <code className="font-mono">python -m agentguard.sdk.demo_traffic</code> to send synthetic traffic
                through the gateway.
              </EmptyState>
            )
          ) : (
            <Skeleton className="h-[250px]" />
          )}
        </Card>

        <div className="min-w-0 space-y-4">
          <Card title="Why calls were denied" subtitle="Top reasons in this window">
            {data ? (
              <BarList
                color="var(--critical)"
                items={Object.entries(data.denials_by_reason).map(([reason, value]) => ({
                  key: reason,
                  label: reasonLabel(reason),
                  value,
                }))}
                empty={<p className="text-[13px] text-ink-3">Nothing denied in this window.</p>}
              />
            ) : (
              <Skeleton className="h-28" />
            )}
          </Card>
          <Card
            title="Needs attention"
            actions={
              <Link to="/insights" className="text-xs font-medium text-accent-ink hover:underline">
                View all
              </Link>
            }
          >
            <ul className="space-y-3 text-[13px]">
              <li className="flex items-center gap-2.5">
                <Inbox aria-hidden className="size-4 text-ink-3" />
                <span className="flex-1 text-ink-2">Pending approvals</span>
                <span className="tabular font-semibold">{data?.pending_approvals ?? "—"}</span>
              </li>
              <li className="flex items-center gap-2.5">
                <Radar aria-hidden className="size-4 text-ink-3" />
                <span className="flex-1 text-ink-2">Anomaly findings</span>
                <span className="tabular font-semibold">{anomalies.data?.length ?? "—"}</span>
              </li>
              <li className="flex items-center gap-2.5">
                <Timer aria-hidden className="size-4 text-ink-3" />
                <span className="flex-1 text-ink-2">Unresolved timeouts</span>
                <span className="tabular font-semibold">{data?.unresolved_timeouts ?? "—"}</span>
              </li>
              {data?.posture.allow_reads_when_policy_unavailable && (
                <li className="flex items-start gap-2.5 rounded-lg bg-warning-soft p-2.5 text-warning-ink">
                  <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
                  Read-only fallback is on: reads are allowed if policy is unavailable.
                </li>
              )}
            </ul>
          </Card>
        </div>
      </div>

      <Card
        className="mt-4"
        title="Latest activity"
        padded={false}
        actions={
          <Link to="/activity" className="text-xs font-medium text-accent-ink hover:underline">
            Open activity log
          </Link>
        }
      >
        {data ? (
          data.recent.length ? (
            <ExecutionTable rows={data.recent} onSelect={(id) => navigate(`/activity/${id}`)} compact />
          ) : (
            <EmptyState icon={Waypoints} title="Nothing yet" />
          )
        ) : (
          <Skeleton className="m-5 h-40" />
        )}
      </Card>
    </>
  );
}
