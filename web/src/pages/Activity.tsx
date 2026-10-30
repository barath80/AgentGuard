import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Search, Waypoints } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ExecutionDrawer } from "../components/ExecutionDrawer";
import { ExecutionTable } from "../components/ExecutionTable";
import { Button, Card, EmptyState, ErrorState, PageHeader, Segmented, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { formatNumber } from "../lib/format";

const PAGE = 25;

const DECISIONS = [
  { value: "", label: "All" },
  { value: "allow", label: "Allowed" },
  { value: "approval_required", label: "Approval" },
  { value: "deny", label: "Denied" },
];

const OUTCOMES = [
  ["", "Any outcome"],
  ["success", "Succeeded"],
  ["denied", "Not run (denied)"],
  ["not_attempted", "Awaiting"],
  ["failure", "Failed"],
  ["timeout", "Unresolved timeout"],
];

function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

export function Activity() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const decision = params.get("decision") ?? "";
  const outcome = params.get("outcome") ?? "";
  const tool = params.get("tool") ?? "";
  const agentId = params.get("agent_id") ?? "";
  const page = Number(params.get("page") ?? 0);
  const [search, setSearch] = useState(params.get("q") ?? "");
  const q = useDebounced(search);

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setParams(next, { replace: true });
  };

  useEffect(() => {
    if ((params.get("q") ?? "") !== q) update("q", q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const tools = useQuery({ queryKey: ["tools"], queryFn: api.tools, staleTime: 60_000 });
  const executions = useQuery({
    queryKey: ["executions", decision, outcome, tool, agentId, q, page],
    queryFn: () =>
      api.executions({ decision, outcome, tool, agent_id: agentId, q, limit: PAGE, offset: page * PAGE }),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });
  const total = executions.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const select = (executionId: string) => navigate({ pathname: `/activity/${executionId}`, search: params.toString() });

  return (
    <>
      <PageHeader
        title="Activity"
        description="Every tool call, its decision, and what actually happened. Select a call to see its full decision path and audit trail."
      />

      <Card padded={false}>
        <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-center">
          <label className="relative block flex-1">
            <span className="sr-only">Search</span>
            <Search aria-hidden className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-3" />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by execution id, target or reason"
              className="h-9 w-full rounded-lg border border-line-strong bg-surface pl-9 pr-3 text-[13px] outline-none placeholder:text-ink-3 focus:border-accent focus:ring-3 focus:ring-accent/20"
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Segmented label="Decision" value={decision} options={DECISIONS} onChange={(v) => update("decision", v)} />
            <select
              aria-label="Outcome"
              value={outcome}
              onChange={(e) => update("outcome", e.target.value)}
              className="h-9 rounded-lg border border-line-strong bg-surface px-2.5 text-[13px] text-ink outline-none focus:border-accent"
            >
              {OUTCOMES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <select
              aria-label="Tool"
              value={tool}
              onChange={(e) => update("tool", e.target.value)}
              className="h-9 rounded-lg border border-line-strong bg-surface px-2.5 text-[13px] text-ink outline-none focus:border-accent"
            >
              <option value="">Any tool</option>
              {tools.data?.map((t) => (
                <option key={t.id} value={t.name}>
                  {t.name}
                </option>
              ))}
            </select>
            {agentId && (
              <Button size="sm" variant="ghost" onClick={() => update("agent_id", "")}>
                Clear agent filter
              </Button>
            )}
          </div>
        </div>

        {executions.error && (
          <div className="p-4">
            <ErrorState error={executions.error} />
          </div>
        )}
        {executions.data ? (
          executions.data.items.length ? (
            <ExecutionTable rows={executions.data.items} onSelect={select} selectedId={id} />
          ) : (
            <EmptyState icon={Waypoints} title="No matching tool calls">
              Try widening the filters.
            </EmptyState>
          )
        ) : (
          <div className="space-y-2 p-5">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        )}

        <div className="flex items-center justify-between gap-3 border-t border-line px-5 py-3 text-[13px] text-ink-3">
          <span className="tabular">
            {total ? `${formatNumber(page * PAGE + 1)}–${formatNumber(Math.min(total, (page + 1) * PAGE))} of ${formatNumber(total)}` : "0 results"}
          </span>
          <div className="flex gap-2">
            <Button size="sm" icon={ChevronLeft} disabled={page === 0} onClick={() => update("page", String(page - 1))}>
              Previous
            </Button>
            <Button size="sm" disabled={page + 1 >= pages} onClick={() => update("page", String(page + 1))}>
              Next <ChevronRight aria-hidden className="size-4" />
            </Button>
          </div>
        </div>
      </Card>

      {id && <ExecutionDrawer id={id} onClose={() => navigate({ pathname: "/activity", search: params.toString() })} />}
    </>
  );
}
