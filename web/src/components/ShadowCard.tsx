import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Eye, FileInput, Square } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type Rule, type ShadowReport } from "../lib/api";
import { formatPercent, timeAgo } from "../lib/format";
import { Badge, Button, Card, DecisionBadge, EmptyState, ErrorState, Skeleton } from "./ui";

function Stat({ label, value, tone }: { label: string; value: string; tone?: "critical" | "warning" | "good" }) {
  const color = tone === "critical" ? "text-critical-ink" : tone === "warning" ? "text-warning-ink" : tone === "good" ? "text-good-ink" : "text-ink";
  return (
    <div className="min-w-0 rounded-lg border border-line bg-surface-2 px-3 py-2.5">
      <p className="truncate text-xs text-ink-3">{label}</p>
      <p className={`tabular mt-0.5 text-lg font-semibold ${color}`}>{value}</p>
    </div>
  );
}

function Report({ report, canOperate, onLoadRules }: { report: ShadowReport; canOperate: boolean; onLoadRules: (rules: Rule[], description?: string) => void }) {
  const queryClient = useQueryClient();
  const stop = useMutation({
    mutationFn: api.stopShadow,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["shadow"] }),
  });
  const shadow = report.shadow!;
  const compared = report.compared ?? 0;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {report.running ? <Badge tone="accent" icon={Eye}>Running</Badge> : <Badge>Stopped</Badge>}
        <p className="min-w-0 text-[13px] text-ink-2">
          {shadow.description || "Unnamed proposal"} · started {timeAgo(shadow.started_at)} by {shadow.started_by}
          {shadow.stopped_at && ` · stopped ${timeAgo(shadow.stopped_at)} by ${shadow.stopped_by}`}
        </p>
        <div className="ml-auto flex flex-wrap gap-2">
          <Button size="sm" variant="ghost" icon={FileInput} onClick={() => onLoadRules(shadow.rules, shadow.description ?? undefined)}>
            Open in simulation lab
          </Button>
          {canOperate && report.running && (
            <Button size="sm" variant="danger" icon={Square} loading={stop.isPending} onClick={() => stop.mutate()}>
              Stop shadow
            </Button>
          )}
        </div>
      </div>
      {stop.error && <ErrorState error={stop.error} />}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Live calls compared" value={compared.toLocaleString()} />
        <Stat label="Agreement" value={formatPercent(report.agreement, 1)} tone={report.agreement === 1 ? "good" : undefined} />
        <Stat label="Would allow more" value={String(report.looser ?? 0)} tone={report.looser ? "critical" : "good"} />
        <Stat label="Would restrict more" value={String(report.stricter ?? 0)} tone={report.stricter ? "warning" : "good"} />
      </div>

      {compared === 0 ? (
        <p className="text-[13px] text-ink-3">No live calls have reached policy since the shadow started. Results appear as traffic arrives.</p>
      ) : report.differences && report.differences.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-[13px]">
            <thead>
              <tr className="border-b border-line text-xs text-ink-3">
                <th className="py-2 pr-3 font-medium">Live call</th>
                <th className="px-3 py-2 font-medium">Live decision</th>
                <th className="px-3 py-2 font-medium">Shadow would</th>
                <th className="px-3 py-2 font-medium">Difference</th>
                <th className="py-2 pl-3 text-right font-medium">When</th>
              </tr>
            </thead>
            <tbody>
              {report.differences.map((d) => (
                <tr key={d.id} className="border-b border-line last:border-0">
                  <td className="py-2.5 pr-3">
                    <Link to={`/activity/${d.id}`} className="font-mono text-[12px] text-ink hover:underline">
                      {d.tool_name}
                    </Link>
                    <p className="text-xs text-ink-3">
                      {d.agent_name} · {d.target_resource}
                    </p>
                  </td>
                  <td className="px-3 py-2.5">
                    <DecisionBadge decision={d.decision} />
                  </td>
                  <td className="px-3 py-2.5">
                    <span className="inline-flex items-center gap-1.5">
                      <ArrowRight aria-hidden className="size-3.5 text-ink-3" />
                      <DecisionBadge decision={d.shadow_decision} />
                    </span>
                  </td>
                  <td className="px-3 py-2.5">
                    <Badge tone={d.direction === "looser" ? "critical" : "warning"}>
                      {d.direction === "looser" ? "loosens security" : "blocks work"}
                    </Badge>
                  </td>
                  <td className="tabular py-2.5 pl-3 text-right text-ink-3">{timeAgo(d.requested_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-[13px] text-good-ink">Every live call so far got the same decision from both policies.</p>
      )}
    </div>
  );
}

/** Policy shadow mode: a proposal runs beside the live policy on real traffic and only observes. */
export function ShadowCard({ canOperate, onLoadRules }: { canOperate: boolean; onLoadRules: (rules: Rule[], description?: string) => void }) {
  const shadow = useQuery({ queryKey: ["shadow"], queryFn: api.shadow, refetchInterval: 15_000 });
  return (
    <Card
      title={
        <span className="inline-flex items-center gap-1.5">
          <Eye aria-hidden className="size-4 text-accent" /> Shadow mode
        </span>
      }
      subtitle="A proposed policy evaluated beside the live one on real traffic. It records what it would have decided and never changes a live decision."
      className="mt-4"
    >
      {shadow.error && <ErrorState error={shadow.error} />}
      {!shadow.data ? (
        <Skeleton className="h-28" />
      ) : shadow.data.shadow ? (
        <Report report={shadow.data} canOperate={canOperate} onLoadRules={onLoadRules} />
      ) : (
        <EmptyState icon={Eye} title="No shadow policy yet">
          Simulate a proposal in the lab above, then choose “Run in shadow” to watch it against live traffic before publishing.
        </EmptyState>
      )}
    </Card>
  );
}
