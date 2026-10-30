import clsx from "clsx";
import { CheckCircle2, Hourglass, XCircle, type LucideIcon } from "lucide-react";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { Decision, SeriesBucket } from "../lib/api";
import { DECISION_LABEL, formatNumber } from "../lib/format";

// Decisions are states, so they take the reserved status colors (always paired with icon + label).
export const DECISION_SERIES: { key: Decision; color: string; icon: LucideIcon }[] = [
  { key: "allow", color: "var(--good)", icon: CheckCircle2 },
  { key: "approval_required", color: "var(--warning)", icon: Hourglass },
  { key: "deny", color: "var(--critical)", icon: XCircle },
];

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

function niceMax(value: number): number {
  if (value <= 4) return 4;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((s) => s * magnitude * 4 >= value) ?? 10;
  return step * magnitude * 4;
}

/** A bar segment with only its top corners rounded, so the stack stays anchored to the baseline. */
function topRounded(x: number, y: number, w: number, h: number, r: number): string {
  const radius = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + radius}Q${x},${y} ${x + radius},${y}H${x + w - radius}Q${x + w},${y} ${x + w},${y + radius}V${y + h}Z`;
}

function bucketLabel(iso: string, windowHours: number): string {
  const d = new Date(iso);
  if (windowHours > 24) return d.toLocaleDateString(undefined, { weekday: "short", hour: "2-digit" });
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function Legend({ items }: { items: { label: string; color: string; icon?: LucideIcon }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
      {items.map(({ label, color, icon: Icon }) => (
        <li key={label} className="inline-flex items-center gap-1.5">
          <span aria-hidden className="size-2.5 rounded-[3px]" style={{ background: color }} />
          {Icon && <Icon aria-hidden className="size-3.5 text-ink-3" />}
          {label}
        </li>
      ))}
    </ul>
  );
}

export function DecisionTimeline({ series, windowHours }: { series: SeriesBucket[]; windowHours: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const height = 220;
  const margin = { top: 8, right: 4, bottom: 22, left: 30 };
  const plotW = Math.max(0, width - margin.left - margin.right);
  const plotH = height - margin.top - margin.bottom;
  const totals = series.map((b) => b.allow + b.deny + b.approval_required);
  const max = niceMax(Math.max(0, ...totals));
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => Math.round(t * max));
  const slot = series.length ? plotW / series.length : 0;
  const barW = Math.max(2, Math.min(28, slot * 0.64));
  const y = (v: number) => margin.top + plotH - (v / max) * plotH;
  const labelEvery = Math.ceil(series.length / Math.max(1, Math.floor(plotW / 72)));
  const hovered = hover !== null ? series[hover] : null;

  return (
    <div>
      <div className="mb-3">
        <Legend items={DECISION_SERIES.map((s) => ({ label: DECISION_LABEL[s.key], color: s.color, icon: s.icon }))} />
      </div>
      <div ref={ref} className="relative" onMouseLeave={() => setHover(null)}>
        {width > 0 && (
          <svg width={width} height={height} role="img" aria-label="Tool-call decisions over time, stacked by decision">
            {ticks.map((t) => (
              <g key={t}>
                <line
                  x1={margin.left}
                  x2={width - margin.right}
                  y1={y(t)}
                  y2={y(t)}
                  stroke={t === 0 ? "var(--chart-axis)" : "var(--chart-grid)"}
                  strokeWidth={1}
                />
                <text x={margin.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="tabular fill-ink-3 text-[11px]">
                  {t}
                </text>
              </g>
            ))}
            {series.map((bucket, i) => {
              const x = margin.left + i * slot + (slot - barW) / 2;
              let base = 0;
              const segments = DECISION_SERIES.filter((s) => bucket[s.key] > 0);
              return (
                <g key={bucket.start} opacity={hover === null || hover === i ? 1 : 0.45}>
                  {segments.map((s, si) => {
                    const top = y(base + bucket[s.key]);
                    const bottom = y(base) - (si > 0 ? 2 : 0); // 2px surface gap between stacked fills
                    base += bucket[s.key];
                    const h = Math.max(1, bottom - top);
                    const isTop = si === segments.length - 1;
                    return isTop ? (
                      <path key={s.key} d={topRounded(x, top, barW, h, 4)} fill={s.color} />
                    ) : (
                      <rect key={s.key} x={x} y={top} width={barW} height={h} fill={s.color} />
                    );
                  })}
                  {i % labelEvery === 0 && (
                    <text x={x + barW / 2} y={height - 6} textAnchor="middle" className="tabular fill-ink-3 text-[11px]">
                      {bucketLabel(bucket.start, windowHours)}
                    </text>
                  )}
                  <rect
                    x={margin.left + i * slot}
                    y={margin.top}
                    width={slot}
                    height={plotH}
                    fill="transparent"
                    onMouseEnter={() => setHover(i)}
                  />
                </g>
              );
            })}
          </svg>
        )}
        {hovered && hover !== null && (
          <div
            role="tooltip"
            className="pointer-events-none absolute z-10 min-w-40 rounded-lg border border-line bg-surface px-3 py-2 text-xs shadow-pop"
            style={{
              left: Math.min(Math.max(0, margin.left + hover * slot + slot / 2 - 80), Math.max(0, width - 170)),
              top: 0,
            }}
          >
            <p className="mb-1.5 font-medium text-ink">{new Date(hovered.start).toLocaleString()}</p>
            {DECISION_SERIES.map((s) => (
              <p key={s.key} className="flex items-center justify-between gap-4 text-ink-2">
                <span className="inline-flex items-center gap-1.5">
                  <span className="size-2 rounded-[2px]" style={{ background: s.color }} />
                  {DECISION_LABEL[s.key]}
                </span>
                <span className="tabular font-medium text-ink">{hovered[s.key]}</span>
              </p>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Horizontal magnitude bars for a ranked list (single series, so no legend box). */
export function BarList({
  items,
  color = "var(--accent)",
  empty,
}: {
  items: { label: ReactNode; value: number; key: string }[];
  color?: string;
  empty?: ReactNode;
}) {
  const max = Math.max(1, ...items.map((i) => i.value));
  if (!items.length) return <>{empty}</>;
  return (
    <ul className="space-y-2.5">
      {items.map((item) => (
        <li key={item.key} className="group" title={`${item.key}: ${item.value}`}>
          <div className="mb-1 flex items-baseline justify-between gap-3 text-[13px]">
            <span className="min-w-0 truncate text-ink-2 group-hover:text-ink">{item.label}</span>
            <span className="tabular font-medium text-ink">{formatNumber(item.value)}</span>
          </div>
          <div className="h-1.5 rounded-full bg-surface-2">
            <div
              className={clsx("h-full rounded-full transition-[width] duration-500")}
              style={{ width: `${(item.value / max) * 100}%`, background: color }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** Proportion of each decision as one segmented bar with 2px gaps. */
export function DecisionSplit({ counts }: { counts: Partial<Record<Decision, number>> }) {
  const total = DECISION_SERIES.reduce((sum, s) => sum + (counts[s.key] ?? 0), 0);
  if (!total) return <div className="h-2 rounded-full bg-surface-2" />;
  return (
    <div className="flex h-2 gap-0.5" role="img" aria-label="Share of calls by decision">
      {DECISION_SERIES.filter((s) => counts[s.key]).map((s) => (
        <div
          key={s.key}
          className="h-full first:rounded-l-full last:rounded-r-full"
          style={{ width: `${((counts[s.key] ?? 0) / total) * 100}%`, background: s.color }}
          title={`${DECISION_LABEL[s.key]}: ${counts[s.key]}`}
        />
      ))}
    </div>
  );
}
