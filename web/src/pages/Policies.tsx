import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  ArrowRight,
  CheckCircle2,
  Eye,
  FlaskConical,
  History,
  Lock,
  Play,
  RotateCcw,
  Upload,
  ShieldAlert,
  TrendingDown,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ShadowCard } from "../components/ShadowCard";
import { Badge, Button, Card, DecisionBadge, ErrorState, PageHeader, Skeleton } from "../components/ui";
import { api, type PolicyVersion, type ProposedPolicy, type Rule, type SimulationReport } from "../lib/api";
import { describeMatch, formatDateTime, formatPercent } from "../lib/format";
import { useMe } from "../lib/auth";

function RuleList({ rules }: { rules: Rule[] }) {
  return (
    <ol className="divide-y divide-line">
      {rules.map((r) => {
        const conditions = describeMatch(r.match);
        return (
          <li key={r.id ?? r.priority} className="flex gap-4 py-3 first:pt-0">
            <span className="tabular mt-0.5 w-8 shrink-0 font-mono text-xs text-ink-3">#{r.priority}</span>
            <div className="min-w-0 flex-1">
              <p className="text-[13px] leading-6 text-ink-2">
                {conditions.length ? (
                  <>
                    When{" "}
                    {conditions.map((c, i) => (
                      <span key={c}>
                        {i > 0 && " and "}
                        <span className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-ink">{c}</span>
                      </span>
                    ))}
                  </>
                ) : (
                  "Always"
                )}
              </p>
              {r.approval_role && <p className="mt-1 text-xs text-ink-3">Approver role: {r.approval_role}</p>}
            </div>
            <div className="shrink-0">
              <DecisionBadge decision={r.decision} />
            </div>
          </li>
        );
      })}
      <li className="flex gap-4 pt-3 text-[13px] text-ink-3">
        <span className="w-8 shrink-0 font-mono text-xs">—</span>
        <span className="flex-1">Anything not matched above (fail closed)</span>
        <DecisionBadge decision="deny" />
      </li>
    </ol>
  );
}

function toEditable(version: number, rules: Rule[], description?: string) {
  return JSON.stringify(
    {
      version,
      description: description ?? "",
      rules: rules.map(({ priority, decision, approval_role, match }) => ({
        priority,
        decision,
        ...(approval_role ? { approval_role } : {}),
        match,
      })),
    },
    null,
    2,
  );
}

function Metric({
  label,
  value,
  tone,
  icon: Icon,
}: {
  label: string;
  value: string;
  tone?: "good" | "critical" | "warning";
  icon?: LucideIcon;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface p-3">
      <p className="flex items-center gap-1.5 text-xs text-ink-3">
        {Icon && <Icon aria-hidden className="size-3.5" />}
        {label}
      </p>
      <p
        className={clsx(
          "tabular mt-1 text-xl font-semibold",
          tone === "critical" && "text-critical-ink",
          tone === "warning" && "text-warning-ink",
          tone === "good" && "text-good-ink",
          !tone && "text-ink",
        )}
      >
        {value}
      </p>
    </div>
  );
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function SimulationResult({ report }: { report: SimulationReport }) {
  const loosens = report.verdict === "loosens_security";
  return (
    <div className="space-y-4">
      <div
        role="status"
        className={clsx(
          "flex items-start gap-3 rounded-xl p-4 ring-1 ring-inset",
          loosens ? "bg-critical-soft text-critical-ink ring-critical/30" : "bg-good-soft text-good-ink ring-good/30",
        )}
      >
        {loosens ? (
          <ShieldAlert aria-hidden className="mt-0.5 size-5 shrink-0" />
        ) : (
          <CheckCircle2 aria-hidden className="mt-0.5 size-5 shrink-0" />
        )}
        <div>
          <p className="font-semibold">
            {loosens
              ? `v${report.proposed_version} would allow ${plural(report.newly_allowed, "call")} that were blocked or gated`
              : `v${report.proposed_version} doesn't loosen security on recorded traffic`}
          </p>
          <p className="mt-0.5 text-[13px] opacity-90">
            {report.newly_restricted
              ? `${plural(report.newly_restricted, "previously unattended call")} would now be denied or need approval. Check that's intended.`
              : "No legitimate work that ran unattended would be newly blocked."}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric label="Calls evaluated" value={String(report.evaluated)} />
        <Metric label="Decisions changed" value={String(report.changes.length)} />
        <Metric
          label="Security loosening"
          value={String(report.newly_allowed)}
          tone={report.newly_allowed ? "critical" : "good"}
          icon={ShieldAlert}
        />
        <Metric
          label="Task-success risk"
          value={String(report.newly_restricted)}
          tone={report.newly_restricted ? "warning" : "good"}
          icon={TrendingDown}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface p-3 text-[13px]">
        <span className="text-ink-3">Unattended completion</span>
        <span className="tabular font-semibold text-ink">{formatPercent(report.unattended_rate_before)}</span>
        <ArrowRight aria-hidden className="size-4 text-ink-3" />
        <span className="tabular font-semibold text-ink">{formatPercent(report.unattended_rate_after)}</span>
        <span className="text-xs text-ink-3 sm:ml-auto">
          {plural(report.skipped_pre_policy, "identity-scope denial")} skipped — policy can't change them
        </span>
      </div>

      {report.changes.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-line">
          <table className="w-full min-w-[620px] text-left text-[13px]">
            <thead className="bg-surface-2 text-xs text-ink-3">
              <tr>
                <th className="px-4 py-2 font-medium">Recorded call</th>
                <th className="px-3 py-2 font-medium">Was</th>
                <th className="px-3 py-2 font-medium">Would be</th>
                <th className="px-4 py-2 font-medium">Impact</th>
              </tr>
            </thead>
            <tbody className="bg-surface">
              {report.changes.map((c) => (
                <tr key={c.execution_id} className="border-t border-line">
                  <td className="px-4 py-2.5">
                    <Link to={`/activity/${c.execution_id}`} className="font-mono text-[12.5px] text-ink hover:underline">
                      {c.tool_name}
                    </Link>
                    <p className="text-xs text-ink-3">
                      {c.agent_name} · {c.environment} · {c.target_resource}
                    </p>
                  </td>
                  <td className="px-3 py-2.5">
                    <DecisionBadge decision={c.recorded} />
                  </td>
                  <td className="px-3 py-2.5">
                    <DecisionBadge decision={c.simulated} />
                  </td>
                  <td className="px-4 py-2.5">
                    {c.impact === "security_loosening" ? (
                      <Badge tone="critical" icon={ShieldAlert}>
                        Loosens security
                      </Badge>
                    ) : c.impact === "task_success_risk" ? (
                      <Badge tone="warning" icon={TrendingDown}>
                        Blocks routine work
                      </Badge>
                    ) : (
                      <Badge>Changes gating</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PublishPanel({
  report,
  policy,
  nextVersion,
  onPublished,
}: {
  report: SimulationReport;
  policy: { description?: string; rules: Rule[] };
  nextVersion: number;
  onPublished: (version: number) => void;
}) {
  const queryClient = useQueryClient();
  const loosens = report.newly_allowed > 0;
  const [acknowledged, setAcknowledged] = useState(false);
  const [reason, setReason] = useState("");
  const publish = useMutation({
    mutationFn: () =>
      api.publishPolicy({
        description: policy.description,
        rules: policy.rules,
        acknowledge_loosening: loosens && acknowledged,
        reason: loosens ? reason : undefined,
      }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["policies"] });
      queryClient.invalidateQueries({ queryKey: ["overview"] });
      onPublished(result.version);
    },
  });
  const ready = !loosens || (acknowledged && reason.trim().length >= 10);

  return (
    <div className="mt-5 rounded-xl border border-line bg-surface-2 p-4">
      <p className="font-medium text-ink">Publish as v{nextVersion}</p>
      <p className="mt-1 text-[13px] text-ink-3">
        Becomes the active policy immediately. The server re-runs this simulation before publishing, and pending
        approvals issued under the current version will need re-approval.
      </p>
      {loosens && (
        <div className="mt-3 space-y-2">
          <label className="flex items-start gap-2 text-[13px] text-critical-ink">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
              className="mt-0.5 size-4 accent-[var(--color-critical)]"
            />
            I understand this allows {plural(report.newly_allowed, "recorded call")} that were denied or gated.
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-ink-2">Reason (kept in the operator log)</span>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={2}
              className="w-full rounded-lg border border-line-strong bg-surface p-2 text-[13px] text-ink outline-none focus:border-accent focus:ring-3 focus:ring-accent/20"
            />
          </label>
        </div>
      )}
      {publish.error && <p role="alert" className="mt-3 text-[13px] text-critical-ink">{publish.error.message}</p>}
      <Button
        variant={loosens ? "danger" : "primary"}
        icon={Upload}
        className="mt-3"
        loading={publish.isPending}
        disabled={!ready}
        onClick={() => publish.mutate()}
      >
        Publish v{nextVersion}
      </Button>
    </div>
  );
}

function ShadowStart({ policy }: { policy: { description?: string; rules: Rule[] } }) {
  const queryClient = useQueryClient();
  const start = useMutation({
    mutationFn: () => api.startShadow({ description: policy.description, rules: policy.rules }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["shadow"] }),
  });
  return (
    <div className="mt-3 rounded-xl border border-dashed border-line-strong p-4">
      <p className="font-medium text-ink">Or watch it on live traffic first</p>
      <p className="mt-1 text-[13px] text-ink-3">
        Shadow mode evaluates these rules beside the live policy on every new call and records where they'd differ. Nothing
        changes for agents.
      </p>
      {start.error && <p role="alert" className="mt-2 text-[13px] text-critical-ink">{start.error.message}</p>}
      {start.isSuccess ? (
        <p role="status" className="mt-3 flex items-center gap-1.5 text-[13px] text-good-ink">
          <Eye aria-hidden className="size-4" /> Running in shadow. Results build up under Shadow mode below.
        </p>
      ) : (
        <Button icon={Eye} className="mt-3" loading={start.isPending} onClick={() => start.mutate()}>
          Run in shadow
        </Button>
      )}
    </div>
  );
}

function SimulationLab({
  active,
  proposed,
  canPublish,
  nextVersion,
  injected,
}: {
  active?: PolicyVersion;
  proposed: ProposedPolicy[];
  canPublish: boolean;
  nextVersion: number;
  injected?: { text: string; nonce: number };
}) {
  const options = useMemo(
    () => [
      ...proposed.map((p) => ({
        key: p.name,
        label: `Proposal: ${p.name}`,
        text: toEditable(p.policy.version, p.policy.rules, p.description ?? undefined),
      })),
      ...(active
        ? [{ key: "active", label: `Draft from active v${active.version}`, text: toEditable(active.version + 1, active.rules) }]
        : []),
      ...(injected ? [{ key: "shadow", label: "From shadow mode", text: injected.text }] : []),
    ],
    [active, proposed, injected],
  );
  const [source, setSource] = useState("");
  const [text, setText] = useState("");
  const simulate = useMutation({ mutationFn: (policy: unknown) => api.simulate(policy) });
  // Publishing is offered only for exactly the text that was simulated.
  const [simulatedText, setSimulatedText] = useState<string | null>(null);
  const [published, setPublished] = useState<number | null>(null);

  useEffect(() => {
    if (injected) {
      setSource("shadow");
      setText(injected.text);
      simulate.reset();
    }
    // Runs once per injected nonce, not on every render.
  }, [injected?.nonce]);

  useEffect(() => {
    if (!source && options.length) {
      setSource(options[0].key);
      setText(options[0].text);
    }
  }, [options, source]);

  const parsed = useMemo(() => {
    try {
      return { value: JSON.parse(text) as unknown, error: null };
    } catch (e) {
      return { value: null, error: (e as Error).message };
    }
  }, [text]);

  const load = (key: string) => {
    const option = options.find((o) => o.key === key);
    setSource(key);
    if (option) setText(option.text);
    simulate.reset();
  };

  return (
    <Card
      title={
        <span className="inline-flex items-center gap-1.5">
          <FlaskConical aria-hidden className="size-4 text-accent" /> Counterfactual simulation
        </span>
      }
      subtitle="Evaluate a proposed policy against every recorded call. Read-only — nothing is executed or written."
    >
      <div className="grid gap-5 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <select
              aria-label="Starting point"
              value={source}
              onChange={(e) => load(e.target.value)}
              className="h-9 min-w-0 flex-1 rounded-lg border border-line-strong bg-surface px-2.5 text-[13px] text-ink outline-none focus:border-accent"
            >
              {options.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </select>
            <Button icon={RotateCcw} variant="ghost" onClick={() => load(source)}>
              Reset
            </Button>
          </div>
          <label className="block">
            <span className="sr-only">Proposed policy JSON</span>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              spellCheck={false}
              rows={22}
              className={clsx(
                "w-full resize-y rounded-lg border bg-surface-2 p-3 font-mono text-[12px] leading-relaxed text-ink outline-none focus:ring-3",
                parsed.error ? "border-critical focus:ring-critical/20" : "border-line focus:border-accent focus:ring-accent/20",
              )}
            />
          </label>
          {parsed.error && <p className="text-xs text-critical-ink">Invalid JSON: {parsed.error}</p>}
          <Button
            variant="primary"
            icon={Play}
            loading={simulate.isPending}
            disabled={!!parsed.error}
            onClick={() => {
              setSimulatedText(text);
              setPublished(null);
              simulate.mutate(parsed.value);
            }}
            className="w-full"
          >
            Run simulation
          </Button>
        </div>
        <div className="min-w-0">
          {simulate.error && <ErrorState error={simulate.error} />}
          {published !== null && (
            <div role="status" className="mb-4 flex items-center gap-2 rounded-lg bg-good-soft px-3 py-2 text-[13px] text-good-ink">
              <CheckCircle2 aria-hidden className="size-4" /> Published v{published}; it's now the active policy.
            </div>
          )}
          {simulate.data ? (
            <>
              <SimulationResult report={simulate.data} />
              {canPublish && published === null && (
                simulatedText === text ? (
                  <>
                    <PublishPanel
                      report={simulate.data}
                      policy={parsed.value as { description?: string; rules: Rule[] }}
                      nextVersion={nextVersion}
                      onPublished={(version) => {
                        setPublished(version);
                        simulate.reset();
                      }}
                    />
                    <ShadowStart policy={parsed.value as { description?: string; rules: Rule[] }} />
                  </>
                ) : (
                  <p className="mt-5 text-[13px] text-ink-3">The policy changed since this simulation. Run it again to publish.</p>
                )
              )}
            </>
          ) : (
            !simulate.error && (
              <div className="grid h-full min-h-60 place-items-center rounded-xl border border-dashed border-line-strong p-8 text-center">
                <div>
                  <FlaskConical aria-hidden className="mx-auto mb-3 size-8 text-ink-3" />
                  <p className="font-medium text-ink">Assess a policy change before release</p>
                  <p className="mx-auto mt-1 max-w-sm text-[13px] text-ink-3">
                    Results separate security loosening (blocked work that would now run) from task-success risk
                    (routine work that would now be blocked).
                  </p>
                </div>
              </div>
            )
          )}
        </div>
      </div>
    </Card>
  );
}

function ReplayCard() {
  const replay = useMutation({ mutationFn: api.replay });
  const r = replay.data;
  return (
    <Card
      title={
        <span className="inline-flex items-center gap-1.5">
          <History aria-hidden className="size-4 text-accent" /> Deterministic replay
        </span>
      }
      subtitle="Re-derive each recorded decision under the policy version that made it, with dry-run tools."
      actions={
        <Button size="sm" icon={Play} loading={replay.isPending} onClick={() => replay.mutate()}>
          Run
        </Button>
      }
    >
      {replay.error && <ErrorState error={replay.error} />}
      {r ? (
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            {r.mismatches.length === 0 ? (
              <CheckCircle2 aria-hidden className="size-6 text-good-ink" />
            ) : (
              <ShieldAlert aria-hidden className="size-6 text-critical-ink" />
            )}
            <p className="text-[15px] font-semibold">
              <span className="tabular">
                {r.reproduced}/{r.replayed}
              </span>{" "}
              decisions reproduced
            </p>
          </div>
          <ul className="space-y-1 text-[13px] text-ink-2">
            {Object.entries(r.by_policy_version).map(([version, v]) => (
              <li key={version} className="flex justify-between">
                <span>Policy v{version}</span>
                <span className="tabular">
                  {v.reproduced}/{v.replayed}
                </span>
              </li>
            ))}
          </ul>
          {r.mismatches.map((m) => (
            <p key={m.execution_id} className="text-[13px] text-critical-ink">
              <Link to={`/activity/${m.execution_id}`} className="underline">
                {m.tool_name}
              </Link>{" "}
              recorded {m.recorded}, replayed {m.replayed ?? "— (version missing)"}
            </p>
          ))}
        </div>
      ) : (
        <p className="text-[13px] text-ink-3">
          Confirms the audit trail is explainable: every past decision follows from the policy version it names.
        </p>
      )}
    </Card>
  );
}

export function Policies() {
  const me = useMe();
  const [injected, setInjected] = useState<{ text: string; nonce: number } | undefined>();
  const policies = useQuery({ queryKey: ["policies"], queryFn: api.policies });
  const versions = policies.data?.versions ?? [];
  const active = versions.find((v) => v.is_active);
  const [selected, setSelected] = useState<number | null>(null);
  const shown = versions.find((v) => v.version === selected) ?? active;

  return (
    <>
      <PageHeader
        title="Policies & simulation"
        description="Versioned, deterministic rules evaluated in priority order. The first match wins; anything unmatched is denied."
      />
      {policies.error && <ErrorState error={policies.error} />}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card
          title={shown ? `Policy v${shown.version}` : "Policy"}
          subtitle={shown ? `${shown.scope} scope · published ${formatDateTime(shown.published_at)}` : undefined}
          actions={
            <div className="flex items-center gap-2">
              {shown?.is_active && <Badge tone="good">Active</Badge>}
              {versions.length > 1 && (
                <select
                  aria-label="Policy version"
                  value={shown?.version}
                  onChange={(e) => setSelected(Number(e.target.value))}
                  className="h-7 rounded-md border border-line-strong bg-surface px-2 text-xs"
                >
                  {versions.map((v) => (
                    <option key={v.id} value={v.version}>
                      v{v.version}
                      {v.is_active ? " (active)" : ""}
                    </option>
                  ))}
                </select>
              )}
            </div>
          }
        >
          {shown ? <RuleList rules={shown.rules} /> : <Skeleton className="h-48" />}
        </Card>
        {me.reads_all_tenants && <ReplayCard />}
      </div>

      <div className="mt-4">
        {me.reads_all_tenants ? (
          policies.data ? (
            <SimulationLab
              active={active}
              proposed={policies.data.proposed}
              canPublish={me.can_operate}
              nextVersion={Math.max(0, ...versions.map((v) => v.version)) + 1}
              injected={injected}
            />
          ) : (
            <Skeleton className="h-96 rounded-xl" />
          )
        ) : (
          <Card>
            <div className="flex items-center gap-3 text-[13px] text-ink-2">
              <Lock aria-hidden className="size-4 text-ink-3" />
              Simulation and replay cover every tenant's traffic, so they need all-tenant read access.
            </div>
          </Card>
        )}
        {me.reads_all_tenants && (
          <ShadowCard
            canOperate={me.can_operate}
            onLoadRules={(rules, description) => {
              setInjected({ text: toEditable(Math.max(0, ...versions.map((v) => v.version)) + 1, rules, description), nonce: Date.now() });
              window.scrollTo({ top: 0, behavior: "smooth" });
            }}
          />
        )}
      </div>
    </>
  );
}
