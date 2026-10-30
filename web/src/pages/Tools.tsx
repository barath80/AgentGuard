import { useQuery } from "@tanstack/react-query";
import { KeyRound, Wrench } from "lucide-react";
import { Badge, Card, EmptyState, ErrorState, Mono, PageHeader, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { formatNumber } from "../lib/format";

const RISK = {
  read: { tone: "good", label: "Read" },
  "write-low": { tone: "accent", label: "Write · low" },
  "write-high": { tone: "warning", label: "Write · high" },
  destructive: { tone: "critical", label: "Destructive" },
} as const;

export function Tools() {
  const tools = useQuery({ queryKey: ["tools"], queryFn: api.tools });
  return (
    <>
      <PageHeader
        title="Tools"
        description="What agents can ask for. Each tool has a versioned input schema, a risk class policy rules match on, and one executor with a single-scope credential only the broker holds."
      />
      {tools.error && <ErrorState error={tools.error} />}
      {!tools.data ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-60 rounded-xl" />
          ))}
        </div>
      ) : tools.data.length === 0 ? (
        <Card>
          <EmptyState icon={Wrench} title="No tools registered" />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {tools.data.map((t) => {
            const risk = RISK[t.risk_class as keyof typeof RISK] ?? { tone: "neutral" as const, label: t.risk_class };
            const props = Object.entries(t.input_schema?.properties ?? {});
            return (
              <Card key={t.id}>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-mono text-[15px] font-semibold text-ink">{t.name}</p>
                    <p className="mt-0.5 text-xs text-ink-3">
                      schema v{t.schema_version} · scope <Mono>{t.target_scope}</Mono>
                    </p>
                  </div>
                  <Badge tone={risk.tone}>{risk.label}</Badge>
                </div>
                <div className="mt-4">
                  <p className="mb-1.5 text-xs font-medium text-ink-3">Accepted arguments</p>
                  <ul className="divide-y divide-line rounded-lg border border-line">
                    {props.map(([name, spec]) => (
                      <li key={name} className="flex items-center justify-between px-3 py-1.5 text-[13px]">
                        <span className="font-mono text-ink">{name}</span>
                        <span className="flex items-center gap-2 text-xs text-ink-3">
                          {t.spend_arg === name && <Badge tone="accent">counts toward budget</Badge>}
                          {spec.type}
                        </span>
                      </li>
                    ))}
                  </ul>
                  {(t.risk_class === "write-high" || t.risk_class === "destructive") && (
                    <p className="mt-2 text-xs text-ink-3">Unknown arguments are rejected for this risk class.</p>
                  )}
                </div>
                <div className="mt-4 flex items-center justify-between border-t border-line pt-3 text-xs text-ink-3">
                  <span className="inline-flex items-center gap-1.5">
                    <KeyRound aria-hidden className="size-3.5" />
                    <Mono>{t.executor_ref}</Mono>
                  </span>
                  <span className="tabular">{formatNumber(t.calls_total)} calls</span>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </>
  );
}
