import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { CheckCircle2, GitBranch, Hourglass, MinusCircle, OctagonAlert, XCircle, type LucideIcon } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Workflow, type WorkflowStatus, type WorkflowStep } from "../lib/api";
import { timeAgo } from "../lib/format";
import { Badge, Card, EmptyState, ErrorState, Segmented, Skeleton } from "./ui";

const STATUS: Record<WorkflowStatus, { label: string; tone: "critical" | "warning" | "neutral" | "good"; hint: string }> = {
  partial: { label: "Partial", tone: "critical", hint: "Some writes happened, later steps didn't. Decide what to finish or undo." },
  in_progress: { label: "Waiting", tone: "warning", hint: "A step is waiting for a human approval." },
  incomplete: { label: "Stopped", tone: "neutral", hint: "Stopped before anything was written. Safe to retry." },
  completed: { label: "Completed", tone: "good", hint: "Every step succeeded." },
};

const STEP: Record<WorkflowStep["state"], { icon: LucideIcon; className: string; label: string }> = {
  done: { icon: CheckCircle2, className: "text-good", label: "done" },
  waiting: { icon: Hourglass, className: "text-warning", label: "waiting for approval" },
  blocked: { icon: MinusCircle, className: "text-critical", label: "blocked" },
  failed: { icon: XCircle, className: "text-critical", label: "failed" },
  unconfirmed: { icon: OctagonAlert, className: "text-warning", label: "timed out, unconfirmed" },
};

function WorkflowRow({ workflow }: { workflow: Workflow }) {
  const status = STATUS[workflow.status];
  return (
    <li className="py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Badge tone={status.tone}>{status.label}</Badge>
        <span className="font-medium text-ink">{workflow.agent_name}</span>
        <span className="min-w-0 truncate font-mono text-xs text-ink-3">{workflow.workflow_id}</span>
        <span className="ml-auto text-xs text-ink-3">{timeAgo(workflow.last_step_at)}</span>
      </div>
      <ol className="mt-2.5 flex flex-wrap items-center gap-1.5" aria-label="Steps">
        {workflow.steps.map((s, i) => {
          const meta = STEP[s.state];
          return (
            <li key={s.execution_id} className="flex items-center gap-1.5">
              {i > 0 && <span aria-hidden className="h-px w-3 bg-line-strong" />}
              <Link
                to={`/activity/${s.execution_id}`}
                title={`${s.tool_name}: ${meta.label}${s.reason ? ` (${s.reason})` : ""}`}
                className="inline-flex items-center gap-1 rounded-md border border-line bg-surface-2 px-2 py-1 text-xs text-ink-2 hover:border-line-strong hover:text-ink"
              >
                <meta.icon aria-hidden className={clsx("size-3.5", meta.className)} />
                <span className="font-mono">{s.tool_name}</span>
                <span className="sr-only">{meta.label}</span>
              </Link>
            </li>
          );
        })}
      </ol>
      {workflow.status === "partial" && workflow.side_effects.length > 0 && (
        <p className="mt-2 text-xs text-ink-2">
          Already changed:{" "}
          {workflow.side_effects.map((s, i) => (
            <span key={s.execution_id}>
              {i > 0 && ", "}
              <span className="font-mono">{s.tool_name}</span> on <span className="font-mono">{s.target_resource}</span>
              {s.state === "unconfirmed" && " (unconfirmed)"}
            </span>
          ))}
        </p>
      )}
    </li>
  );
}

/** Multi-step tasks that stopped part-way (THREAT_MODEL #18). */
export function WorkflowsCard() {
  const [status, setStatus] = useState<WorkflowStatus>("partial");
  const workflows = useQuery({
    queryKey: ["workflows", status],
    queryFn: () => api.workflows(status),
    refetchInterval: 60_000,
  });
  return (
    <Card
      title={
        <span className="inline-flex items-center gap-1.5">
          <GitBranch aria-hidden className="size-4 text-accent" /> Workflows
        </span>
      }
      subtitle={STATUS[status].hint}
      actions={
        <Segmented
          label="Workflow status"
          value={status}
          onChange={setStatus}
          options={[
            { value: "partial", label: "Partial" },
            { value: "in_progress", label: "Waiting" },
            { value: "incomplete", label: "Stopped" },
            { value: "completed", label: "Done" },
          ]}
        />
      }
      className="mb-4"
    >
      {workflows.error && <ErrorState error={workflows.error} />}
      {!workflows.data ? (
        <Skeleton className="h-24" />
      ) : workflows.data.length ? (
        <ul className="divide-y divide-line">
          {workflows.data.slice(0, 8).map((w) => (
            <WorkflowRow key={`${w.agent_id}-${w.workflow_id}`} workflow={w} />
          ))}
        </ul>
      ) : (
        <EmptyState icon={GitBranch} title={`No ${STATUS[status].label.toLowerCase()} workflows in the last 24 hours`}>
          Agents group the steps of one task with a workflow id; anything that stops half-way is listed here.
        </EmptyState>
      )}
    </Card>
  );
}
