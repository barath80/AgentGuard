import clsx from "clsx";
import type { ExecutionSummary } from "../lib/api";
import { formatMs, timeAgo } from "../lib/format";
import { DecisionBadge, OutcomeBadge } from "./ui";

export function ExecutionTable({
  rows,
  onSelect,
  selectedId,
  compact,
}: {
  rows: ExecutionSummary[];
  onSelect: (id: string) => void;
  selectedId?: string;
  compact?: boolean;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-left text-[13px]">
        <thead>
          <tr className="border-b border-line text-xs text-ink-3">
            <th className="px-5 py-2.5 font-medium">Time</th>
            <th className="px-3 py-2.5 font-medium">Agent</th>
            <th className="px-3 py-2.5 font-medium">Tool · target</th>
            <th className="px-3 py-2.5 font-medium">Env</th>
            <th className="px-3 py-2.5 font-medium">Decision</th>
            <th className="px-3 py-2.5 font-medium">Outcome</th>
            {!compact && <th className="px-3 py-2.5 font-medium">Reason</th>}
            <th className="px-5 py-2.5 text-right font-medium">Latency</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => (
            <tr
              key={e.id}
              tabIndex={0}
              onClick={() => onSelect(e.id)}
              onKeyDown={(ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  onSelect(e.id);
                }
              }}
              aria-selected={selectedId === e.id}
              className={clsx(
                "cursor-pointer border-b border-line last:border-0 outline-none transition hover:bg-surface-hover focus-visible:bg-surface-hover",
                selectedId === e.id && "bg-accent-soft/60",
              )}
            >
              <td className="whitespace-nowrap px-5 py-2.5 text-ink-2" title={e.requested_at ?? ""}>
                {timeAgo(e.requested_at)}
              </td>
              <td className="px-3 py-2.5">
                <p className="font-medium text-ink">{e.agent_name}</p>
                <p className="text-xs text-ink-3">{e.tenant_id}</p>
              </td>
              <td className="px-3 py-2.5">
                <p className="font-mono text-[12.5px] text-ink">{e.tool_name}</p>
                <p className="max-w-[220px] truncate text-xs text-ink-3">{e.target_resource}</p>
              </td>
              <td className="px-3 py-2.5">
                <span
                  className={clsx(
                    "rounded px-1.5 py-0.5 font-mono text-[11px]",
                    e.environment === "prod" ? "bg-critical-soft text-critical-ink" : "bg-surface-2 text-ink-2",
                  )}
                >
                  {e.environment}
                </span>
              </td>
              <td className="px-3 py-2.5">
                <DecisionBadge decision={e.decision} />
              </td>
              <td className="px-3 py-2.5">
                <OutcomeBadge outcome={e.outcome} />
              </td>
              {!compact && (
                <td className="max-w-[280px] truncate px-3 py-2.5 text-ink-2" title={e.reason ?? ""}>
                  {e.reason ?? <span className="text-ink-3">—</span>}
                </td>
              )}
              <td className="tabular whitespace-nowrap px-5 py-2.5 text-right text-ink-2">{formatMs(e.latency_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
