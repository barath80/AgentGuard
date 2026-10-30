import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Check, Inbox, Lock, ShieldQuestion, Timer, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, EmptyState, ErrorState, Hash, PageHeader, Segmented, Skeleton } from "../components/ui";
import { api, type Approval } from "../lib/api";
import { formatDateTime, timeAgo, timeUntil } from "../lib/format";

function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

const STATUS_TONE = {
  pending: "warning",
  approved: "accent",
  consumed: "good",
  denied: "critical",
  expired: "neutral",
} as const;

function ApprovalCard({ approval, now }: { approval: Approval; now: number }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<null | boolean>(null);
  const decide = useMutation({
    mutationFn: (approve: boolean) => api.decide(approval.id, approve),
    onSuccess: () => {
      setConfirming(null);
      queryClient.invalidateQueries({ queryKey: ["approvals"] });
      queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
  });

  const total = approval.expires_at && approval.created_at
    ? new Date(approval.expires_at).getTime() - new Date(approval.created_at).getTime()
    : 1;
  const remaining = approval.expires_at ? new Date(approval.expires_at).getTime() - now : 0;
  const remainingShare = Math.max(0, Math.min(1, remaining / total));
  const expired = approval.status === "pending" && remaining <= 0;
  const status = expired ? "expired" : approval.status;
  const args = approval.args ?? null;

  return (
    <article className="rounded-xl border border-line bg-surface shadow-card">
      <div className="flex flex-col gap-4 p-5 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[14px] font-semibold text-ink">{approval.tool_name}</span>
            <Badge tone={STATUS_TONE[status]}>{status}</Badge>
            {approval.environment && <Badge>{approval.environment}</Badge>}
          </div>
          <p className="text-[13px] text-ink-2">
            Requested by <span className="font-medium text-ink">{approval.agent_name ?? "unknown agent"}</span> in{" "}
            {approval.tenant_id} · {timeAgo(approval.created_at, now)}
          </p>
        </div>
        {approval.status === "pending" && !expired && (
          <div className="flex shrink-0 items-center gap-2 text-[13px] text-warning-ink">
            <Timer aria-hidden className="size-4" />
            <span className="tabular font-medium">{timeUntil(approval.expires_at, now)}</span>
          </div>
        )}
      </div>

      {approval.status === "pending" && !expired && (
        <div className="mx-5 h-1 overflow-hidden rounded-full bg-surface-2" aria-hidden>
          <div
            className={clsx("h-full rounded-full transition-[width] duration-1000", remainingShare < 0.2 ? "bg-critical" : "bg-warning")}
            style={{ width: `${remainingShare * 100}%` }}
          />
        </div>
      )}

      <div className="grid gap-4 p-5 md:grid-cols-2">
        <div>
          <p className="mb-1.5 text-xs font-medium text-ink-3">Requested action</p>
          {args ? (
            <dl className="space-y-1 rounded-lg bg-surface-2 p-3 text-[13px]">
              {Object.entries(args).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-3">
                  <dt className="text-ink-3">{k}</dt>
                  <dd className="tabular truncate font-mono text-ink">{String(v)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="rounded-lg bg-surface-2 p-3 text-[13px] text-ink-3">Arguments hidden for your role.</p>
          )}
        </div>
        <div>
          <p className="mb-1.5 text-xs font-medium text-ink-3">Bound to — any change invalidates this approval</p>
          <dl className="space-y-1 rounded-lg bg-surface-2 p-3 text-[13px]">
            <div className="flex justify-between gap-3">
              <dt className="text-ink-3">Action hash</dt>
              <dd>
                <Hash value={approval.action_hash} length={16} />
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-ink-3">Resource</dt>
              <dd className="truncate font-mono text-ink">{approval.resource}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-ink-3">Policy version</dt>
              <dd className="tabular font-mono text-ink">v{approval.policy_version}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-ink-3">Required role</dt>
              <dd className="font-mono text-ink">{approval.required_role ?? "any"}</dd>
            </div>
          </dl>
        </div>
      </div>

      <footer className="flex flex-col gap-3 border-t border-line px-5 py-3.5 sm:flex-row sm:items-center sm:justify-between">
        <Link to={`/activity/${approval.execution_id}`} className="text-[13px] font-medium text-accent-ink hover:underline">
          View decision path
        </Link>
        {approval.status === "pending" && !expired ? (
          approval.can_decide ? (
            confirming === null ? (
              <div className="flex gap-2">
                <Button icon={X} onClick={() => setConfirming(false)}>
                  Reject
                </Button>
                <Button variant="success" icon={Check} onClick={() => setConfirming(true)}>
                  Approve
                </Button>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] text-ink-2">
                  {confirming ? "Approve this exact action, once?" : "Reject this request?"}
                </span>
                <Button size="sm" variant="ghost" onClick={() => setConfirming(null)} disabled={decide.isPending}>
                  Cancel
                </Button>
                <Button
                  size="sm"
                  variant={confirming ? "success" : "danger"}
                  loading={decide.isPending}
                  onClick={() => decide.mutate(confirming)}
                >
                  {confirming ? "Confirm approval" : "Confirm rejection"}
                </Button>
              </div>
            )
          ) : (
            <span className="inline-flex items-center gap-1.5 text-[13px] text-ink-3">
              <Lock aria-hidden className="size-3.5" /> You can't decide this: {approval.blocked_reason}
            </span>
          )
        ) : (
          <span className="text-[13px] text-ink-3">
            {approval.approver ? `${status} by ${approval.approver} · ${formatDateTime(approval.decided_at)}` : status}
          </span>
        )}
      </footer>
      {decide.error && (
        <div className="px-5 pb-4">
          <ErrorState error={decide.error} />
        </div>
      )}
    </article>
  );
}

export function Approvals() {
  const [tab, setTab] = useState<"pending" | "all">("pending");
  const now = useNow();
  const approvals = useQuery({
    queryKey: ["approvals", tab],
    queryFn: () => api.approvals(tab === "pending" ? "pending" : undefined),
    refetchInterval: 60_000,
  });
  const items = (approvals.data ?? []).filter(
    (a) => tab === "all" || !a.expires_at || new Date(a.expires_at).getTime() > now,
  );

  return (
    <>
      <PageHeader
        title="Approvals"
        description="High-risk calls wait here for a human. Each approval is bound to one exact action and policy version, expires, and can be consumed only once."
        actions={
          <Segmented
            label="Show"
            value={tab}
            onChange={setTab}
            options={[
              { value: "pending", label: "Pending" },
              { value: "all", label: "History" },
            ]}
          />
        }
      />
      {approvals.error && <ErrorState error={approvals.error} />}
      {!approvals.data ? (
        <div className="space-y-3">
          <Skeleton className="h-56 rounded-xl" />
          <Skeleton className="h-56 rounded-xl" />
        </div>
      ) : items.length ? (
        <div className="grid gap-4 2xl:grid-cols-2">
          {items.map((a) => (
            <ApprovalCard key={a.id} approval={a} now={now} />
          ))}
        </div>
      ) : (
        <Card>
          <EmptyState icon={tab === "pending" ? Inbox : ShieldQuestion} title={tab === "pending" ? "Queue is clear" : "No approvals yet"}>
            {tab === "pending"
              ? "Nothing is waiting for a decision. New requests appear here automatically."
              : "Approvals appear once a policy rule requires one."}
          </EmptyState>
        </Card>
      )}
    </>
  );
}
