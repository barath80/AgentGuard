import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Activity,
  Bot,
  FlaskConical,
  Gauge,
  Inbox,
  LogOut,
  Menu,
  Monitor,
  Moon,
  Radar,
  ShieldCheck,
  ShieldOff,
  Sun,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../lib/api";
import { useAuth, useMe } from "../lib/auth";
import { useLiveUpdates, type LiveStatus } from "../lib/live";

type ThemePref = "system" | "light" | "dark";
const THEME_KEY = "agentguard.console.theme";

function useTheme() {
  const [pref, setPref] = useState<ThemePref>(() => {
    try {
      return (localStorage.getItem(THEME_KEY) as ThemePref) || "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (pref === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", pref);
    try {
      localStorage.setItem(THEME_KEY, pref);
    } catch {
      /* per-viewer convenience only */
    }
  }, [pref]);
  return [pref, setPref] as const;
}

const NAV: { to: string; label: string; icon: LucideIcon }[] = [
  { to: "/", label: "Overview", icon: Gauge },
  { to: "/activity", label: "Activity", icon: Activity },
  { to: "/approvals", label: "Approvals", icon: Inbox },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/tools", label: "Tools", icon: Wrench },
  { to: "/policies", label: "Policies & simulation", icon: FlaskConical },
  { to: "/insights", label: "Insights", icon: Radar },
];

function Brand() {
  return (
    <div className="flex items-center gap-2.5 px-2">
      <div className="grid size-8 place-items-center rounded-lg bg-accent text-on-accent shadow-card">
        <ShieldCheck aria-hidden className="size-[18px]" />
      </div>
      <div className="leading-tight">
        <p className="text-[14px] font-semibold text-ink">AgentGuard</p>
        <p className="text-[11px] text-ink-3">Runtime authorization</p>
      </div>
    </div>
  );
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const me = useMe();
  const { signOut } = useAuth();
  const [theme, setTheme] = useTheme();
  const pending = useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: () => api.approvals("pending"),
    refetchInterval: 60_000,
  });
  const actionable = (pending.data ?? []).filter((a) => a.can_decide).length;

  return (
    <div className="flex h-full flex-col gap-6 px-3 py-5">
      <Brand />
      <nav aria-label="Primary" className="flex flex-col gap-0.5">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            onClick={onNavigate}
            className={({ isActive }) =>
              clsx(
                "flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[13px] font-medium transition",
                isActive ? "bg-surface text-ink shadow-card ring-1 ring-line" : "text-ink-2 hover:bg-surface-hover hover:text-ink",
              )
            }
          >
            <Icon aria-hidden className="size-4 shrink-0" />
            <span className="flex-1 truncate">{label}</span>
            {to === "/approvals" && actionable > 0 && (
              <span className="tabular rounded-full bg-warning px-1.5 text-[11px] font-semibold text-black">{actionable}</span>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="mt-auto space-y-3">
        <div role="radiogroup" aria-label="Theme" className="flex rounded-lg bg-surface-2 p-0.5 ring-1 ring-inset ring-line">
          {(
            [
              ["system", Monitor],
              ["light", Sun],
              ["dark", Moon],
            ] as const
          ).map(([value, Icon]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={theme === value}
              aria-label={`${value} theme`}
              onClick={() => setTheme(value)}
              className={clsx(
                "grid h-7 flex-1 place-items-center rounded-md transition",
                theme === value ? "bg-surface text-ink shadow-card ring-1 ring-line" : "text-ink-3 hover:text-ink",
              )}
            >
              <Icon aria-hidden className="size-3.5" />
            </button>
          ))}
        </div>
        <div className="rounded-xl border border-line bg-surface p-3 shadow-card">
          <div className="flex items-center gap-2.5">
            <div className="grid size-8 shrink-0 place-items-center rounded-full bg-accent-soft text-[13px] font-semibold uppercase text-accent-ink">
              {me.name.slice(0, 2)}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-[13px] font-medium text-ink">{me.name}</p>
              <p className="truncate text-[11px] text-ink-3">
                {me.roles.join(", ")} · {me.reads_all_tenants ? "all tenants" : me.tenants.join(", ")}
              </p>
            </div>
            <button
              type="button"
              onClick={signOut}
              aria-label="Sign out"
              title="Sign out"
              className="grid size-7 place-items-center rounded-md text-ink-3 hover:bg-surface-hover hover:text-ink"
            >
              <LogOut aria-hidden className="size-4" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

const LIVE_STYLE: Record<LiveStatus, { dot: string; label: string }> = {
  live: { dot: "bg-good animate-pulse", label: "Live" },
  connecting: { dot: "bg-warning", label: "Connecting" },
  offline: { dot: "bg-ink-3", label: "Offline · refreshing every minute" },
};

function LivePill({ status }: { status: LiveStatus }) {
  const style = LIVE_STYLE[status];
  return (
    <span
      role="status"
      className="ml-auto inline-flex items-center gap-1.5 rounded-md bg-surface px-2 py-1 text-xs text-ink-2 ring-1 ring-inset ring-line"
    >
      <span aria-hidden className={clsx("size-1.5 rounded-full", style.dot)} />
      {style.label}
    </span>
  );
}

function PostureBar() {
  const overview = useQuery({ queryKey: ["overview", 24], queryFn: () => api.overview(24), refetchInterval: 60_000 });
  const posture = overview.data?.posture;
  if (!posture) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="inline-flex items-center gap-1.5 rounded-md bg-surface px-2 py-1 font-medium text-ink-2 ring-1 ring-inset ring-line">
        <span className="size-1.5 rounded-full bg-accent" />
        {posture.environment}
      </span>
      <span className="inline-flex items-center gap-1.5 rounded-md bg-surface px-2 py-1 text-ink-2 ring-1 ring-inset ring-line">
        Policy <span className="tabular font-semibold text-ink">v{posture.active_policy_version ?? "—"}</span>
      </span>
      {posture.kill_switch ? (
        <span className="inline-flex items-center gap-1.5 rounded-md bg-critical-soft px-2 py-1 font-semibold text-critical-ink ring-1 ring-inset ring-critical/30">
          <ShieldOff aria-hidden className="size-3.5" /> Kill switch engaged — all calls denied
        </span>
      ) : (
        <span className="inline-flex items-center gap-1.5 rounded-md bg-good-soft px-2 py-1 font-medium text-good-ink ring-1 ring-inset ring-good/25">
          <ShieldCheck aria-hidden className="size-3.5" /> Enforcing · fail-closed
        </span>
      )}
    </div>
  );
}

export function Layout() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const live = useLiveUpdates();
  useEffect(() => setOpen(false), [location.pathname]);

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[248px_1fr]">
      <aside className="sticky top-0 hidden h-screen border-r border-line bg-page lg:block">
        <Sidebar />
      </aside>

      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <div className="absolute inset-0 bg-black/40" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-72 border-r border-line bg-page shadow-pop">
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close navigation"
              className="absolute right-3 top-5 grid size-8 place-items-center rounded-md text-ink-3 hover:bg-surface-hover"
            >
              <X aria-hidden className="size-4" />
            </button>
            <Sidebar onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}

      <div className="min-w-0">
        <div className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-line bg-page/85 px-4 backdrop-blur sm:px-6 lg:px-8">
          <button
            type="button"
            onClick={() => setOpen(true)}
            aria-label="Open navigation"
            className="grid size-9 place-items-center rounded-lg text-ink-2 hover:bg-surface-hover lg:hidden"
          >
            <Menu aria-hidden className="size-5" />
          </button>
          <PostureBar />
          <LivePill status={live} />
        </div>
        <main className="mx-auto max-w-[1400px] px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
