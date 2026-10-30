import clsx from "clsx";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Clock3,
  Copy,
  Hourglass,
  Loader2,
  MinusCircle,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import type { Decision } from "../lib/api";
import { DECISION_LABEL } from "../lib/format";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <h1 className="text-[22px] font-semibold tracking-tight text-ink">{title}</h1>
        {description && <p className="mt-1 max-w-2xl text-ink-2">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
  padded = true,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  return (
    <section className={clsx("min-w-0 rounded-xl border border-line bg-surface shadow-card", className)}>
      {(title || actions) && (
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-3.5">
          <div className="min-w-0">
            {title && <h2 className="text-[13px] font-semibold text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-ink-3">{subtitle}</p>}
          </div>
          {actions}
        </div>
      )}
      <div className={clsx(padded && "p-5")}>{children}</div>
    </section>
  );
}

type Tone = "neutral" | "good" | "warning" | "critical" | "accent";

const TONE: Record<Tone, string> = {
  neutral: "bg-surface-2 text-ink-2 ring-line",
  good: "bg-good-soft text-good-ink ring-good/25",
  warning: "bg-warning-soft text-warning-ink ring-warning/35",
  critical: "bg-critical-soft text-critical-ink ring-critical/25",
  accent: "bg-accent-soft text-accent-ink ring-accent/25",
};

export function Badge({ tone = "neutral", icon: Icon, children }: { tone?: Tone; icon?: LucideIcon; children: ReactNode }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset",
        TONE[tone],
      )}
    >
      {Icon && <Icon aria-hidden className="size-3.5" />}
      {children}
    </span>
  );
}

const DECISION_TONE: Record<Decision, { tone: Tone; icon: LucideIcon }> = {
  allow: { tone: "good", icon: CheckCircle2 },
  deny: { tone: "critical", icon: XCircle },
  approval_required: { tone: "warning", icon: Hourglass },
};

export function DecisionBadge({ decision }: { decision: Decision }) {
  const { tone, icon } = DECISION_TONE[decision] ?? { tone: "neutral", icon: MinusCircle };
  return (
    <Badge tone={tone} icon={icon}>
      {DECISION_LABEL[decision] ?? decision}
    </Badge>
  );
}

const OUTCOME: Record<string, { tone: Tone; icon: LucideIcon; label: string }> = {
  success: { tone: "good", icon: Check, label: "Succeeded" },
  denied: { tone: "neutral", icon: MinusCircle, label: "Not run" },
  failure: { tone: "critical", icon: XCircle, label: "Failed" },
  timeout: { tone: "warning", icon: AlertTriangle, label: "Unresolved timeout" },
  not_attempted: { tone: "neutral", icon: Clock3, label: "Awaiting" },
};

export function OutcomeBadge({ outcome }: { outcome: string }) {
  const o = OUTCOME[outcome] ?? { tone: "neutral" as Tone, icon: MinusCircle, label: outcome };
  return (
    <Badge tone={o.tone} icon={o.icon}>
      {o.label}
    </Badge>
  );
}

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "success";

const BUTTON: Record<ButtonVariant, string> = {
  primary: "bg-accent text-on-accent hover:brightness-110 shadow-card",
  secondary: "bg-surface text-ink ring-1 ring-inset ring-line-strong hover:bg-surface-hover shadow-card",
  ghost: "text-ink-2 hover:bg-surface-hover hover:text-ink",
  danger: "bg-critical text-white hover:brightness-110 shadow-card",
  success: "bg-good text-white hover:brightness-110 shadow-card",
};

export function Button({
  variant = "secondary",
  size = "md",
  icon: Icon,
  loading,
  children,
  className,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  icon?: LucideIcon;
  loading?: boolean;
}) {
  return (
    <button
      type="button"
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5 text-[13px]",
        BUTTON[variant],
        className,
      )}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <Loader2 aria-hidden className="size-4 animate-spin" /> : Icon && <Icon aria-hidden className="size-4" />}
      {children}
    </button>
  );
}

export function Segmented<T extends string | number>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-lg bg-surface-2 p-0.5 ring-1 ring-inset ring-line">
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          onClick={() => onChange(o.value)}
          className={clsx(
            "h-7 rounded-md px-3 text-xs font-medium transition",
            o.value === value ? "bg-surface text-ink shadow-card ring-1 ring-line" : "text-ink-3 hover:text-ink",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Hash({ value, length = 10 }: { value: string | null | undefined; length?: number }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <span className="text-ink-3">—</span>;
  const display = value.replace(/^sha256:/, "");
  return (
    <button
      type="button"
      title={`${value} — click to copy`}
      onClick={() => {
        navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
      className="group inline-flex items-center gap-1 rounded font-mono text-xs text-ink-2 hover:text-ink"
    >
      {display.slice(0, length)}
      {display.length > length && "…"}
      {copied ? (
        <Check aria-label="Copied" className="size-3 text-good-ink" />
      ) : (
        <Copy aria-hidden className="size-3 opacity-0 transition group-hover:opacity-100" />
      )}
    </button>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded-md bg-surface-2", className)} />;
}

export function EmptyState({ icon: Icon, title, children }: { icon: LucideIcon; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="mb-3 grid size-10 place-items-center rounded-full bg-surface-2 text-ink-3">
        <Icon aria-hidden className="size-5" />
      </div>
      <p className="font-medium text-ink">{title}</p>
      {children && <div className="mt-1 max-w-sm text-[13px] text-ink-3">{children}</div>}
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  return (
    <div role="alert" className="flex items-start gap-2 rounded-lg bg-critical-soft px-4 py-3 text-[13px] text-critical-ink">
      <XCircle aria-hidden className="mt-0.5 size-4 shrink-0" />
      <span>{error instanceof Error ? error.message : "Something went wrong."}</span>
    </div>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(110px,auto)_1fr] gap-x-4 gap-y-2.5 text-[13px]">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-ink-3">{k}</dt>
          <dd className="min-w-0 break-words text-ink">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-[12.5px]">{children}</span>;
}
