import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Bot, CirclePause, CirclePlay, Zap } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, EmptyState, ErrorState, Mono, PageHeader, Skeleton } from "../components/ui";
import { api, type Agent } from "../lib/api";
import { formatNumber, timeAgo } from "../lib/format";
import { useMe } from "../lib/auth";

function SpendMeter({ agent }: { agent: Agent }) {
  if (agent.daily_spend_limit === null) return <span className="text-[13px] text-ink-3">No budget</span>;
  const share = Math.min(1, agent.spent_24h / agent.daily_spend_limit);
  return (
    <div className="w-36">
      <div className="mb-1 flex justify-between text-xs">
        <span className="tabular text-ink">{formatNumber(agent.spent_24h)}</span>
        <span className="tabular text-ink-3">/ {formatNumber(agent.daily_spend_limit)}</span>
      </div>
      <div className="h-1.5 rounded-full bg-surface-2" role="meter" aria-valuenow={share * 100} aria-valuemin={0} aria-valuemax={100} aria-label="Spend used in the last 24 hours">
        <div
          className={clsx("h-full rounded-full", share >= 0.9 ? "bg-critical" : share >= 0.7 ? "bg-warning" : "bg-accent")}
          style={{ width: `${share * 100}%` }}
        />
      </div>
    </div>
  );
}

function SuspendButton({ agent }: { agent: Agent }) {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const suspended = agent.status === "suspended";
  const mutation = useMutation({
    mutationFn: () => api.setSuspended(agent.id, !suspended),
    onSuccess: () => {
      setConfirm(false);
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
  if (suspended) {
    return (
      <Button size="sm" icon={CirclePlay} loading={mutation.isPending} onClick={() => mutation.mutate()}>
        Resume
      </Button>
    );
  }
  return confirm ? (
    <div className="flex items-center justify-end gap-1.5">
      <Button size="sm" variant="ghost" onClick={() => setConfirm(false)}>
        Cancel
      </Button>
      <Button size="sm" variant="danger" loading={mutation.isPending} onClick={() => mutation.mutate()}>
        Suspend now
      </Button>
    </div>
  ) : (
    <Button size="sm" icon={CirclePause} onClick={() => setConfirm(true)}>
      Suspend
    </Button>
  );
}

export function Agents() {
  const me = useMe();
  const agents = useQuery({ queryKey: ["agents"], queryFn: api.agents, refetchInterval: 60_000 });

  return (
    <>
      <PageHeader
        title="Agents"
        description="Registered agent identities, the environments they may act in, and their runtime limits. Suspending an agent blocks its very next call."
      />
      {agents.error && <ErrorState error={agents.error} />}
      <Card padded={false}>
        {!agents.data ? (
          <div className="space-y-2 p-5">
            <Skeleton className="h-14" />
            <Skeleton className="h-14" />
          </div>
        ) : agents.data.length === 0 ? (
          <EmptyState icon={Bot} title="No agents registered" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] text-left text-[13px]">
              <thead>
                <tr className="border-b border-line text-xs text-ink-3">
                  <th className="px-5 py-2.5 font-medium">Agent</th>
                  <th className="px-3 py-2.5 font-medium">Status</th>
                  <th className="px-3 py-2.5 font-medium">Environments</th>
                  <th className="px-3 py-2.5 font-medium">Risk</th>
                  <th className="px-3 py-2.5 font-medium">Calls 24h</th>
                  <th className="px-3 py-2.5 font-medium">Spend 24h</th>
                  <th className="px-3 py-2.5 font-medium">Last seen</th>
                  {me.can_operate && <th className="px-5 py-2.5 text-right font-medium">Control</th>}
                </tr>
              </thead>
              <tbody>
                {agents.data.map((a) => (
                  <tr key={a.id} className="border-b border-line last:border-0">
                    <td className="px-5 py-3">
                      <Link to={`/activity?agent_id=${a.id}`} className="font-medium text-ink hover:underline">
                        {a.name}
                      </Link>
                      <p className="text-xs text-ink-3">
                        <Mono>{a.workload_identity_ref}</Mono>
                      </p>
                      <p className="text-xs text-ink-3">
                        {a.tenant_id} · owned by {a.owner}
                      </p>
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex flex-col items-start gap-1">
                        <Badge tone={a.status === "active" ? "good" : "critical"}>{a.status}</Badge>
                        {a.circuit_open && (
                          <Badge tone="warning" icon={Zap}>
                            circuit open
                          </Badge>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex flex-wrap gap-1">
                        {a.approved_environments.map((env) => (
                          <span key={env} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-ink-2">
                            {env}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="px-3 py-3 capitalize text-ink-2">{a.risk_category}</td>
                    <td className="px-3 py-3">
                      <p className="tabular text-ink">{formatNumber(a.calls_24h)}</p>
                      <p className="tabular text-xs text-ink-3">
                        {formatNumber(a.denials_24h)} denied · {a.rate_limit_per_minute}/min cap
                      </p>
                    </td>
                    <td className="px-3 py-3">
                      <SpendMeter agent={a} />
                    </td>
                    <td className="px-3 py-3 text-ink-2">{timeAgo(a.last_seen_at)}</td>
                    {me.can_operate && (
                      <td className="px-5 py-3 text-right">
                        <SuspendButton agent={a} />
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
